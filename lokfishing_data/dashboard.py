import copy
import hmac
import io
import math
import re
from pathlib import Path

from aiohttp import web


AVATAR_DIR = Path("/home/cogs/lokfishing/avatars")
MAX_IMAGE_BYTES = 10 * 1024 * 1024
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
MAX_DESCRIPTION_LENGTH = 1000
MAX_COLLECTION_FISH = 100
MAX_COLLECTION_PROMPT_LENGTH = 1500

DEFAULT_COLLECTION_PROMPT = (
    "🎉 **{user}**, you completed the **{collection}** collection "
    "at **{location}**!"
)


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "fish"


def _location_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")


def _positive_number(value, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a positive number.") from exc

    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{label} must be a positive number.")

    return number


def _percentage(value, label: str = "Percentage") -> float:
    try:
        percent = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be greater than 0 and at most 100.") from exc

    if not math.isfinite(percent) or not 0 < percent <= 100:
        raise ValueError(f"{label} must be greater than 0 and at most 100.")

    return percent


def _table_rates(table: list) -> list:
    """Return each entry's percentage of the table total."""
    if not table:
        raise ValueError("The drop table is empty.")

    weights = []
    for entry in table:
        try:
            weight = float(entry.get("weight", 0))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("The table contains an invalid entry.") from exc

        if not math.isfinite(weight) or weight <= 0:
            raise ValueError("Table values must be positive numbers.")

        weights.append(weight)

    total = sum(weights)
    return [weight * 100.0 / total for weight in weights]


def _clean_description(value) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("Description must be text.")

    description = value.strip()
    if len(description) > MAX_DESCRIPTION_LENGTH:
        raise ValueError(
            f"Description must be {MAX_DESCRIPTION_LENGTH} characters or fewer."
        )

    return description


def _clean_collection_fish(value) -> list:
    if not isinstance(value, list):
        raise ValueError("Collection fish must be a list.")

    result = []
    seen = set()

    for raw_name in value:
        name = str(raw_name).strip()
        if not name:
            continue
        if len(name) > 80:
            raise ValueError("Fish names must be 80 characters or fewer.")
        if name.casefold() == "nothing":
            raise ValueError("Nothing cannot be a required collection fish.")

        if name.casefold() not in seen:
            result.append(name)
            seen.add(name.casefold())

    if len(result) > MAX_COLLECTION_FISH:
        raise ValueError(
            f"A collection may contain at most {MAX_COLLECTION_FISH} fish."
        )

    return result


def _clean_collection_prompt(value) -> str:
    if not isinstance(value, str):
        raise ValueError("Collection prompt must be text.")

    prompt = value.strip()
    if len(prompt) > MAX_COLLECTION_PROMPT_LENGTH:
        raise ValueError(
            f"Collection prompt must be {MAX_COLLECTION_PROMPT_LENGTH} "
            "characters or fewer."
        )

    return prompt


class FishingDashboard:
    """Password-protected LAN dashboard for the LokFishing cog."""

    def __init__(self, cog, password: str, port: int = 8788):
        self.cog = cog
        self.password = password
        self.port = port
        self.runner = None

        self.app = web.Application(
            client_max_size=MAX_IMAGE_BYTES + 1024 * 1024,
            middlewares=[self._auth_middleware],
        )

        self.app.router.add_get("/", self.index)
        self.app.router.add_get("/api/state", self.get_state)
        self.app.router.add_get("/api/avatar/{filename}", self.get_avatar)

        self.app.router.add_patch(
            "/api/game-settings",
            self.update_game_settings,
        )

        self.app.router.add_post("/api/locations", self.create_location)
        self.app.router.add_patch(
            "/api/locations/{key}",
            self.update_location,
        )
        self.app.router.add_delete(
            "/api/locations/{key}",
            self.delete_location,
        )

        self.app.router.add_post(
            "/api/locations/{key}/entries",
            self.add_entry,
        )
        self.app.router.add_patch("/api/entry", self.update_entry)
        self.app.router.add_delete("/api/entry", self.delete_entry)
        self.app.router.add_patch(
            "/api/fish-description",
            self.update_fish_description,
        )
        self.app.router.add_patch(
            "/api/collections/{key}",
            self.update_collection,
        )

        self.app.router.add_post("/api/avatar", self.upload_avatar)

    @web.middleware
    async def _auth_middleware(self, request, handler):
        """Require the dashboard password for all API routes."""
        if request.path.startswith("/api/"):
            authorization = request.headers.get("Authorization", "")
            supplied = authorization.removeprefix("Bearer ").strip()

            if not supplied or not hmac.compare_digest(
                supplied,
                self.password,
            ):
                return web.json_response(
                    {"error": "Unauthorized"},
                    status=401,
                )

        return await handler(request)

    async def start(self):
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()

        site = web.TCPSite(
            self.runner,
            host="0.0.0.0",
            port=self.port,
        )
        await site.start()

    async def close(self):
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None

    async def index(self, request):
        return web.Response(
            text=DASHBOARD_HTML,
            content_type="text/html",
        )

    def _list_avatars(self):
        """List local image files, excluding symlinks."""
        if not AVATAR_DIR.is_dir():
            return []

        avatars = []
        for path in sorted(
            AVATAR_DIR.iterdir(),
            key=lambda item: item.name.lower(),
        ):
            if (
                path.is_symlink()
                or not path.is_file()
                or path.suffix.lower() not in IMAGE_EXTENSIONS
            ):
                continue

            avatars.append(
                {
                    "filename": path.name,
                    "name": re.sub(
                        r"[-_]+",
                        " ",
                        path.stem,
                    ).strip().title(),
                }
            )

        return avatars

    async def get_state(self, request):
        """Return current settings, tables, collections, and local avatars."""
        locations = copy.deepcopy(await self.cog.config.locations())
        descriptions = await self.cog.config.fish_descriptions()
        collections = copy.deepcopy(await self.cog.config.collections())
        avatars = self._list_avatars()

        game_settings = {
            "casts_per_bait": await self.cog.config.casts_per_bait(),
            "bait_cooldown_seconds": await self.cog.config.bait_cooldown_seconds(),
        }

        avatar_by_slug = {
            _slug(Path(item["filename"]).stem): item["filename"]
            for item in avatars
        }

        for location in locations.values():
            for rarity in ("common", "rare"):
                table = location.get(rarity, [])

                try:
                    rates = _table_rates(table)
                except ValueError:
                    rates = [0.0] * len(table)

                for entry, rate in zip(table, rates):
                    name = str(entry.get("name", "")).strip()

                    if name.casefold() == "nothing":
                        entry["avatar"] = None
                        entry["description"] = ""
                    else:
                        entry["avatar"] = avatar_by_slug.get(_slug(name))
                        entry["description"] = descriptions.get(
                            name.casefold(),
                            "",
                        )

                    # The cog continues storing relative weights internally.
                    entry["rate_percent"] = rate

        return web.json_response(
            {
                "locations": locations,
                "collections": collections,
                "avatars": avatars,
                "game_settings": game_settings,
            }
        )

    async def get_avatar(self, request):
        """Serve only an image present in the local avatar library."""
        filename = request.match_info["filename"]
        allowed = {item["filename"] for item in self._list_avatars()}

        if filename not in allowed:
            return web.json_response(
                {"error": "Avatar not found."},
                status=404,
            )

        return web.FileResponse(AVATAR_DIR / filename)

    async def update_game_settings(self, request):
        """Save per-player casts-per-bait and cooldown settings."""
        try:
            data = await request.json()

            casts_per_bait = int(data.get("casts_per_bait"))
            cooldown_seconds = int(data.get("bait_cooldown_seconds"))

            if not 1 <= casts_per_bait <= 100:
                raise ValueError("Casts per bait must be between 1 and 100.")
            if not 0 <= cooldown_seconds <= 86400:
                raise ValueError(
                    "Bait cooldown must be between 0 and 86400 seconds."
                )

            async with self.cog._state_lock:
                await self.cog.config.casts_per_bait.set(casts_per_bait)
                await self.cog.config.bait_cooldown_seconds.set(
                    cooldown_seconds
                )

            return web.json_response(
                {
                    "ok": True,
                    "casts_per_bait": casts_per_bait,
                    "bait_cooldown_seconds": cooldown_seconds,
                }
            )

        except (ValueError, TypeError, KeyError) as exc:
            return web.json_response({"error": str(exc)}, status=400)

    async def create_location(self, request):
        try:
            data = await request.json()

            label = str(data.get("label", "")).strip()
            common_name = str(data.get("common_fish", "")).strip()
            rare_name = str(data.get("rare_fish", "")).strip()

            try:
                rare_chance = float(data.get("rare_chance", 5))
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "Rare chance must be between 0 and 100."
                ) from exc

            if not label or len(label) > 80:
                raise ValueError("Location name must be 1–80 characters.")
            if not common_name or len(common_name) > 80:
                raise ValueError("Enter an initial common fish name.")
            if not rare_name or len(rare_name) > 80:
                raise ValueError("Enter an initial rare fish name.")
            if common_name.casefold() == "nothing":
                raise ValueError("The initial common entry must be a fish.")
            if rare_name.casefold() == "nothing":
                raise ValueError("The initial rare entry must be a fish.")
            if not math.isfinite(rare_chance) or not 0 <= rare_chance <= 100:
                raise ValueError("Rare chance must be between 0 and 100.")

            key = _location_key(label)
            if not key:
                raise ValueError("Location name must contain letters or numbers.")

            async with self.cog._state_lock:
                locations = await self.cog.config.locations()
                collections = await self.cog.config.collections()

                if key in locations:
                    raise ValueError("A location with that name already exists.")

                locations[key] = {
                    "label": label,
                    "rare_chance": rare_chance,
                    "common": [{"name": common_name, "weight": 1.0}],
                    "rare": [{"name": rare_name, "weight": 1.0}],
                }
                collections[key] = {
                    "enabled": True,
                    "fish": [common_name],
                    "prompt": DEFAULT_COLLECTION_PROMPT,
                }

                await self.cog.config.locations.set(locations)
                await self.cog.config.collections.set(collections)

            return web.json_response({"ok": True, "key": key})

        except (ValueError, TypeError, KeyError) as exc:
            return web.json_response({"error": str(exc)}, status=400)

    async def update_location(self, request):
        key = request.match_info["key"]

        try:
            data = await request.json()

            async with self.cog._state_lock:
                locations = await self.cog.config.locations()
                location = locations.get(key)

                if location is None:
                    return web.json_response(
                        {"error": "Location not found."},
                        status=404,
                    )

                if "label" in data:
                    label = str(data["label"]).strip()
                    if not label or len(label) > 80:
                        raise ValueError(
                            "Location name must be 1–80 characters."
                        )
                    location["label"] = label

                if "rare_chance" in data:
                    try:
                        rare_chance = float(data["rare_chance"])
                    except (TypeError, ValueError) as exc:
                        raise ValueError(
                            "Rare chance must be between 0 and 100."
                        ) from exc

                    if not math.isfinite(rare_chance) or not 0 <= rare_chance <= 100:
                        raise ValueError(
                            "Rare chance must be between 0 and 100."
                        )
                    location["rare_chance"] = rare_chance

                await self.cog.config.locations.set(locations)

            return web.json_response({"ok": True})

        except (ValueError, TypeError, KeyError) as exc:
            return web.json_response({"error": str(exc)}, status=400)

    async def delete_location(self, request):
        key = request.match_info["key"]

        async with self.cog._state_lock:
            locations = await self.cog.config.locations()
            collections = await self.cog.config.collections()

            if key not in locations:
                return web.json_response(
                    {"error": "Location not found."},
                    status=404,
                )
            if len(locations) <= 1:
                return web.json_response(
                    {"error": "Keep at least one location."},
                    status=400,
                )

            label = locations[key]["label"]
            del locations[key]
            collections.pop(key, None)

            await self.cog.config.locations.set(locations)
            await self.cog.config.collections.set(collections)

        return web.json_response({"ok": True, "deleted": label})

    async def add_entry(self, request):
        """Add an entry at a percentage and rescale other table entries."""
        key = request.match_info["key"]

        try:
            data = await request.json()
            rarity = str(data.get("rarity", "")).lower()
            name = str(data.get("name", "")).strip()
            rate = _percentage(data.get("rate_percent"), "Rarity percentage")
            description = _clean_description(data.get("description", ""))
            nothing = bool(data.get("nothing", False))

            if rarity not in {"common", "rare"}:
                raise ValueError("Rarity must be common or rare.")

            if nothing:
                name = "Nothing"
                description = ""
            elif not name or len(name) > 80:
                raise ValueError("Fish name must be 1–80 characters.")
            elif name.casefold() == "nothing":
                raise ValueError(
                    "Select the Nothing option for a no-catch entry."
                )

            async with self.cog._state_lock:
                locations = await self.cog.config.locations()
                location = locations.get(key)

                if location is None:
                    return web.json_response(
                        {"error": "Location not found."},
                        status=404,
                    )

                table = location[rarity]

                if any(
                    str(entry.get("name", "")).casefold() == name.casefold()
                    for entry in table
                ):
                    raise ValueError("That entry is already in this table.")
                if len(table) >= 100:
                    raise ValueError("This table already has 100 entries.")

                if table:
                    if rate >= 100:
                        raise ValueError(
                            "When adding to a non-empty table, the new rate "
                            "must be less than 100%."
                        )

                    old_total = sum(float(entry["weight"]) for entry in table)
                    remaining = 100.0 - rate

                    for entry in table:
                        old_rate = float(entry["weight"]) * 100.0 / old_total
                        entry["weight"] = old_rate * remaining / 100.0
                else:
                    rate = 100.0

                new_entry = {"name": name, "weight": rate}
                if nothing:
                    new_entry["type"] = "nothing"
                table.append(new_entry)

                await self.cog.config.locations.set(locations)

                if description and not nothing:
                    descriptions = await self.cog.config.fish_descriptions()
                    descriptions[name.casefold()] = description
                    await self.cog.config.fish_descriptions.set(descriptions)

            return web.json_response({"ok": True})

        except (ValueError, TypeError, KeyError) as exc:
            return web.json_response({"error": str(exc)}, status=400)

    async def update_entry(self, request):
        """Set one entry's percentage and scale the other entries."""
        key = request.query.get("key", "")
        rarity = request.query.get("rarity", "").lower()
        name = request.query.get("name", "")

        try:
            data = await request.json()
            new_rate = _percentage(
                data.get("rate_percent"),
                "Rarity percentage",
            )

            if rarity not in {"common", "rare"}:
                raise ValueError("Rarity must be common or rare.")

            async with self.cog._state_lock:
                locations = await self.cog.config.locations()
                location = locations.get(key)

                if location is None:
                    return web.json_response(
                        {"error": "Location not found."},
                        status=404,
                    )

                table = location[rarity]
                target = next(
                    (
                        entry for entry in table
                        if str(entry.get("name", "")).casefold()
                        == name.casefold()
                    ),
                    None,
                )

                if target is None:
                    return web.json_response(
                        {"error": "Table entry not found."},
                        status=404,
                    )

                others = [entry for entry in table if entry is not target]

                if not others:
                    if not math.isclose(new_rate, 100.0, abs_tol=0.0001):
                        raise ValueError(
                            "The only entry in a table must have a 100% rate."
                        )
                    target["weight"] = 100.0
                else:
                    if new_rate >= 100:
                        raise ValueError(
                            "An entry cannot be 100% while other entries remain."
                        )

                    other_total = sum(float(entry["weight"]) for entry in others)
                    if not math.isfinite(other_total) or other_total <= 0:
                        raise ValueError("Other table entries have invalid rates.")

                    remaining = 100.0 - new_rate
                    target["weight"] = new_rate

                    for entry in others:
                        entry["weight"] = (
                            float(entry["weight"]) * remaining / other_total
                        )

                await self.cog.config.locations.set(locations)

            return web.json_response({"ok": True})

        except (ValueError, TypeError, KeyError) as exc:
            return web.json_response({"error": str(exc)}, status=400)

    async def delete_entry(self, request):
        """Remove an entry and normalize the remaining entries to 100%."""
        key = request.query.get("key", "")
        rarity = request.query.get("rarity", "").lower()
        name = request.query.get("name", "")

        if rarity not in {"common", "rare"}:
            return web.json_response(
                {"error": "Rarity must be common or rare."},
                status=400,
            )

        async with self.cog._state_lock:
            locations = await self.cog.config.locations()
            location = locations.get(key)

            if location is None:
                return web.json_response(
                    {"error": "Location not found."},
                    status=404,
                )

            table = location[rarity]
            target = next(
                (
                    entry for entry in table
                    if str(entry.get("name", "")).casefold()
                    == name.casefold()
                ),
                None,
            )

            if target is None:
                return web.json_response(
                    {"error": "Table entry not found."},
                    status=404,
                )
            if len(table) <= 1:
                return web.json_response(
                    {"error": "Keep at least one entry in each table."},
                    status=400,
                )

            table.remove(target)
            total = sum(float(entry["weight"]) for entry in table)

            if not math.isfinite(total) or total <= 0:
                return web.json_response(
                    {"error": "Remaining entries have invalid rates."},
                    status=400,
                )

            for entry in table:
                entry["weight"] = float(entry["weight"]) * 100.0 / total

            await self.cog.config.locations.set(locations)

        return web.json_response({"ok": True})

    async def update_fish_description(self, request):
        try:
            data = await request.json()
            name = str(data.get("name", "")).strip()
            description = _clean_description(data.get("description", ""))

            if not name or len(name) > 80:
                raise ValueError("Fish name must be 1–80 characters.")
            if name.casefold() == "nothing":
                raise ValueError("Nothing outcomes cannot have descriptions.")

            async with self.cog._state_lock:
                descriptions = await self.cog.config.fish_descriptions()
                key = name.casefold()

                if description:
                    descriptions[key] = description
                else:
                    descriptions.pop(key, None)

                await self.cog.config.fish_descriptions.set(descriptions)

            return web.json_response({"ok": True})

        except (ValueError, TypeError, KeyError) as exc:
            return web.json_response({"error": str(exc)}, status=400)

    async def update_collection(self, request):
        key = request.match_info["key"]

        try:
            data = await request.json()
            enabled = data.get("enabled")
            fish = _clean_collection_fish(data.get("fish", []))
            prompt = _clean_collection_prompt(data.get("prompt", ""))

            if not isinstance(enabled, bool):
                raise ValueError("Collection enabled must be true or false.")
            if enabled and not fish:
                raise ValueError(
                    "An enabled collection needs at least one required fish."
                )

            async with self.cog._state_lock:
                locations = await self.cog.config.locations()
                if key not in locations:
                    return web.json_response(
                        {"error": "Location not found."},
                        status=404,
                    )

                collections = await self.cog.config.collections()
                collections[key] = {
                    "enabled": enabled,
                    "fish": fish,
                    "prompt": prompt,
                }
                await self.cog.config.collections.set(collections)

            return web.json_response({"ok": True})

        except (ValueError, TypeError, KeyError) as exc:
            return web.json_response({"error": str(exc)}, status=400)

    async def upload_avatar(self, request):
        try:
            form = await request.post()
            fish_name = str(form.get("name", "")).strip()
            image_field = form.get("image")

            if not fish_name or len(fish_name) > 80:
                raise ValueError("Enter a fish name between 1 and 80 characters.")
            if not hasattr(image_field, "file"):
                raise ValueError("Choose an image file.")

            raw_image = image_field.file.read(MAX_IMAGE_BYTES + 1)
            if len(raw_image) > MAX_IMAGE_BYTES:
                raise ValueError("Image must be 10 MB or smaller.")
            if not raw_image:
                raise ValueError("The selected image is empty.")

            from PIL import Image

            with Image.open(io.BytesIO(raw_image)) as image:
                if image.size != (512, 512):
                    raise ValueError("Avatar image must be exactly 512×512 pixels.")
                image.verify()

            with Image.open(io.BytesIO(raw_image)) as image:
                normalized = image.convert("RGBA")
                output = io.BytesIO()
                normalized.save(output, format="PNG")

            AVATAR_DIR.mkdir(parents=True, exist_ok=True)
            slug = _slug(fish_name)
            filename = f"{slug}.png"
            target = AVATAR_DIR / filename
            temporary = AVATAR_DIR / f".{slug}.tmp.png"

            temporary.write_bytes(output.getvalue())
            temporary.replace(target)

            return web.json_response({"ok": True, "filename": filename})

        except (ValueError, TypeError, OSError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except Exception:
            return web.json_response(
                {"error": "Could not process that image."},
                status=400,
            )


DASHBOARD_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LokFishing Dashboard</title>
<style>
  body {font:16px system-ui,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem;background:#101923;color:#edf4fa}
  input,select,button,textarea {font:inherit;padding:.55rem;margin:.2rem;border-radius:6px;border:1px solid #526577}
  button {cursor:pointer;background:#287bb5;color:white}
  input,select,textarea {background:#182632;color:white}
  textarea {box-sizing:border-box;width:100%;min-height:75px;resize:vertical}
  section,.card {background:#17232e;padding:1rem;margin:1rem 0;border-radius:10px}
  .row {display:flex;gap:.5rem;align-items:center;flex-wrap:wrap}
  table {border-collapse:collapse;width:100%;margin:.5rem 0}
  td,th {text-align:left;padding:.5rem;border-bottom:1px solid #40505e;vertical-align:top}
  .small {color:#aebdca}
  .danger {background:#a83d46}
  .fish-icon {width:36px;height:36px;object-fit:cover;border-radius:6px;vertical-align:middle;margin-right:.5rem;background:#263746}
  .description-editor {min-width:220px}
  #error,#loginError {color:#ff9b9b;white-space:pre-wrap}
  #status {color:#a9e6b0}
</style>
</head>
<body>
<h1>LokFishing Dashboard</h1>
<p class="small">Manage bait settings, locations, rarity percentages, fish lore, avatars, and collections.</p>

<div id="login" class="card">
  <label>Dashboard password
    <input id="password" type="password" autocomplete="current-password">
  </label>
  <button id="loginButton">Connect</button>
  <p id="loginError"></p>
</div>

<main id="app" hidden>
  <p id="error"></p>
  <p id="status"></p>

  <section>
    <h2>Bait settings</h2>
    <p class="small">These apply per player across all fishing locations. A Nothing result uses one cast.</p>
    <div class="row">
      <label>Casts per bait
        <input id="castsPerBait" type="number" min="1" max="100" step="1">
      </label>
      <label>Cooldown after bait is used (seconds; 0 disables it)
        <input id="baitCooldown" type="number" min="0" max="86400" step="1">
      </label>
      <button id="saveBaitSettings">Save bait settings</button>
    </div>
  </section>

  <section>
    <h2>Locations</h2>
    <div class="row">
      <select id="locationSelect"></select>
      <input id="labelInput" placeholder="Location label">
      <label>Rare catch chance %
        <input id="rareInput" type="number" min="0" max="100" step="0.1">
      </label>
      <button id="saveLocation">Save location settings</button>
      <button id="deleteLocation" class="danger">Delete location</button>
    </div>
  </section>

  <section>
    <h2>Drop tables</h2>
    <p class="small">
      Entry percentages are within the Common or Rare table and total 100%.
      Editing one entry proportionally adjusts the other entries.
    </p>
    <div id="tables"></div>

    <h3>Add table entry</h3>
    <div class="row">
      <select id="rarity">
        <option value="common">Common</option>
        <option value="rare">Rare</option>
      </select>
      <input id="fishName" placeholder="Fish name">
      <select id="avatarLibrary">
        <option value="">Choose fish from image library…</option>
      </select>
      <label>Chance within table %
        <input id="rate" type="number" min="0.01" max="100" step="0.01" value="10">
      </label>
      <label><input id="nothing" type="checkbox"> Nothing / no catch</label>
    </div>
    <label for="newFishDescription">Optional fish lore description</label>
    <textarea id="newFishDescription" maxlength="1000"
      placeholder="Leave blank for the basic catch message."></textarea>
    <button id="addEntry">Add entry</button>
  </section>

  <section>
    <h2>Collection for this location</h2>
    <label><input id="collectionEnabled" type="checkbox"> Enable this collection</label>
    <p class="small">Enter required fish one per line.</p>
    <label for="collectionFish">Required fish</label>
    <textarea id="collectionFish" placeholder="One fish name per line"></textarea>
    <p id="collectionSuggestions" class="small"></p>
    <label for="collectionPrompt">Completion prompt (tokens: {user}, {collection}, {location})</label>
    <textarea id="collectionPrompt" maxlength="1500"></textarea>
    <button id="saveCollection">Save collection</button>
  </section>

  <section>
    <h2>Upload avatar</h2>
    <p class="small">Upload a square 512×512 image. It stays in the local avatar folder.</p>
    <div class="row">
      <input id="avatarName" placeholder="Fish name">
      <input id="avatarFile" type="file" accept="image/png,image/jpeg,image/webp">
      <button id="uploadAvatar">Upload / replace avatar</button>
    </div>
  </section>

  <section>
    <h2>Add location</h2>
    <div class="row">
      <input id="newLabel" placeholder="Location name">
      <label>Rare catch chance %
        <input id="newRare" type="number" min="0" max="100" step="0.1" value="5">
      </label>
      <input id="newCommon" placeholder="Initial common fish">
      <input id="newRareFish" placeholder="Initial rare fish">
      <button id="createLocation">Create location</button>
    </div>
  </section>

  <button id="logout" class="danger">Log out</button>
</main>

<script>
"use strict";
let token = sessionStorage.getItem("lokfishing-token") || "";
let state = null;
const $ = id => document.getElementById(id);

function showError(message = "") {
  $("error").textContent = message;
  $("loginError").textContent = message;
  $("status").textContent = "";
}
function showStatus(message = "") {
  $("status").textContent = message;
  $("error").textContent = "";
  $("loginError").textContent = "";
}
async function api(url, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set("Authorization", "Bearer " + token);
  let body = options.body;
  if (body && !(body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
    body = JSON.stringify(body);
  }
  const response = await fetch(url, {...options, headers, body});
  const data = await response.json().catch(() => ({error: response.statusText}));
  if (!response.ok) throw new Error(data.error || response.statusText);
  return data;
}
function node(tag, value) {
  const element = document.createElement(tag);
  element.textContent = value;
  return element;
}
async function loadAvatar(img, filename) {
  try {
    const response = await fetch("/api/avatar/" + encodeURIComponent(filename), {
      headers: {Authorization: "Bearer " + token}
    });
    if (!response.ok) return;
    const url = URL.createObjectURL(await response.blob());
    img.onload = () => URL.revokeObjectURL(url);
    img.onerror = () => URL.revokeObjectURL(url);
    img.src = url;
  } catch (error) {}
}
function populateAvatarLibrary() {
  const select = $("avatarLibrary");
  select.replaceChildren(new Option("Choose fish from image library…", ""));
  for (const avatar of (state.avatars || [])) {
    select.add(new Option(`${avatar.name} — ${avatar.filename}`, avatar.filename));
  }
}
function collectionFor(key, location) {
  const saved = (state.collections || {})[key];
  if (saved) return saved;
  return {
    enabled: true,
    fish: (location.common || [])
      .filter(entry => entry.name.toLowerCase() !== "nothing")
      .map(entry => entry.name),
    prompt: ""
  };
}
async function refresh() {
  state = await api("/api/state");

  $("castsPerBait").value = state.game_settings.casts_per_bait;
  $("baitCooldown").value = state.game_settings.bait_cooldown_seconds;

  const select = $("locationSelect");
  const previous = select.value;
  select.replaceChildren();

  for (const [key, location] of Object.entries(state.locations)) {
    select.add(new Option(location.label, key));
  }
  if (previous && state.locations[previous]) select.value = previous;

  populateAvatarLibrary();
  renderLocation();
}
function renderCollection(key, location) {
  const collection = collectionFor(key, location);
  $("collectionEnabled").checked = collection.enabled !== false;
  $("collectionFish").value = (collection.fish || []).join("\n");
  $("collectionPrompt").value = collection.prompt || "";

  const names = [];
  const seen = new Set();
  for (const rarity of ["common", "rare"]) {
    for (const entry of (location[rarity] || [])) {
      if (entry.name.toLowerCase() === "nothing") continue;
      if (!seen.has(entry.name.toLowerCase())) {
        names.push(entry.name);
        seen.add(entry.name.toLowerCase());
      }
    }
  }
  $("collectionSuggestions").textContent = names.length
    ? "Fish in this location's tables: " + names.join(", ")
    : "No fish are currently in this location's tables.";
}
function renderLocation() {
  if (!state) return;
  const key = $("locationSelect").value;
  const location = state.locations[key];

  if (!location) {
    $("tables").replaceChildren(node("p", "Select a location."));
    return;
  }

  $("labelInput").value = location.label;
  $("rareInput").value = location.rare_chance;
  renderCollection(key, location);

  const root = $("tables");
  root.replaceChildren();

  for (const rarity of ["common", "rare"]) {
    root.append(node("h3", rarity[0].toUpperCase() + rarity.slice(1)));
    const table = document.createElement("table");
    const header = document.createElement("tr");

    for (const title of ["Fish / outcome", "Lore description", "Rarity %", "Actions"]) {
      header.append(node("th", title));
    }
    table.append(header);

    for (const entry of (location[rarity] || [])) {
      const row = document.createElement("tr");

      const nameCell = document.createElement("td");
      if (entry.avatar) {
        const img = document.createElement("img");
        img.className = "fish-icon";
        img.alt = entry.name;
        img.title = entry.name;
        nameCell.append(img);
        loadAvatar(img, entry.avatar);
      }
      nameCell.append(document.createTextNode(entry.name));
      row.append(nameCell);

      const descriptionCell = document.createElement("td");
      const descriptionInput = document.createElement("textarea");
      descriptionInput.maxLength = 1000;
      descriptionInput.placeholder = "Optional lore; blank uses the basic catch message.";
      descriptionInput.value = entry.description || "";

      const saveDescription = node("button", "Save description");
      saveDescription.onclick = async () => {
        try {
          await api("/api/fish-description", {
            method: "PATCH",
            body: {name: entry.name, description: descriptionInput.value}
          });
          await refresh();
          showStatus(descriptionInput.value.trim()
            ? "Fish description saved."
            : "Description cleared.");
        } catch (error) {
          showError(error.message);
        }
      };
      descriptionCell.append(descriptionInput, saveDescription);
      row.append(descriptionCell);

      const rateCell = document.createElement("td");
      const rateInput = document.createElement("input");
      rateInput.type = "number";
      rateInput.min = "0.01";
      rateInput.max = "100";
      rateInput.step = "0.01";
      rateInput.value = Number(entry.rate_percent || 0).toFixed(2);
      rateCell.append(rateInput);
      row.append(rateCell);

      const actions = document.createElement("td");
      const saveRate = node("button", "Save %");
      saveRate.onclick = async () => {
        try {
          const query = new URLSearchParams({key, rarity, name: entry.name});
          await api("/api/entry?" + query, {
            method: "PATCH",
            body: {rate_percent: rateInput.value}
          });
          await refresh();
          showStatus("Percentage saved; other entries were adjusted.");
        } catch (error) {
          showError(error.message);
        }
      };

      const remove = node("button", "Remove");
      remove.className = "danger";
      remove.onclick = async () => {
        if (!confirm(`Remove ${entry.name} from the ${rarity} table?`)) return;
        try {
          const query = new URLSearchParams({key, rarity, name: entry.name});
          await api("/api/entry?" + query, {method: "DELETE"});
          await refresh();
          showStatus("Entry removed; remaining percentages were adjusted.");
        } catch (error) {
          showError(error.message);
        }
      };

      actions.append(saveRate, remove);
      row.append(actions);
      table.append(row);
    }
    root.append(table);
  }
}

$("loginButton").onclick = async () => {
  token = $("password").value;
  try {
    await refresh();
    sessionStorage.setItem("lokfishing-token", token);
    $("login").hidden = true;
    $("app").hidden = false;
    showStatus("Connected.");
  } catch (error) {
    token = "";
    showError(error.message);
  }
};
$("password").addEventListener("keydown", event => {
  if (event.key === "Enter") $("loginButton").click();
});
$("locationSelect").onchange = renderLocation;

$("avatarLibrary").onchange = () => {
  const filename = $("avatarLibrary").value;
  const avatar = (state.avatars || []).find(item => item.filename === filename);
  if (avatar) {
    $("fishName").value = avatar.name;
    $("avatarName").value = avatar.name;
  }
};

$("saveBaitSettings").onclick = async () => {
  try {
    const result = await api("/api/game-settings", {
      method: "PATCH",
      body: {
        casts_per_bait: $("castsPerBait").value,
        bait_cooldown_seconds: $("baitCooldown").value
      }
    });
    await refresh();
    showStatus(
      `Saved: ${result.casts_per_bait} casts per bait; ` +
      `${result.bait_cooldown_seconds} second cooldown.`
    );
  } catch (error) {
    showError(error.message);
  }
};

$("saveLocation").onclick = async () => {
  try {
    const key = $("locationSelect").value;
    await api("/api/locations/" + encodeURIComponent(key), {
      method: "PATCH",
      body: {label: $("labelInput").value, rare_chance: $("rareInput").value}
    });
    await refresh();
    showStatus("Location settings saved.");
  } catch (error) {
    showError(error.message);
  }
};

$("deleteLocation").onclick = async () => {
  const key = $("locationSelect").value;
  const location = state.locations[key];
  if (!location || !confirm(`Delete "${location.label}" and its tables?`)) return;

  try {
    await api("/api/locations/" + encodeURIComponent(key), {method: "DELETE"});
    await refresh();
    showStatus("Location deleted.");
  } catch (error) {
    showError(error.message);
  }
};

$("addEntry").onclick = async () => {
  try {
    const key = $("locationSelect").value;
    await api("/api/locations/" + encodeURIComponent(key) + "/entries", {
      method: "POST",
      body: {
        rarity: $("rarity").value,
        name: $("fishName").value,
        rate_percent: $("rate").value,
        description: $("newFishDescription").value,
        nothing: $("nothing").checked
      }
    });
    $("fishName").value = "";
    $("newFishDescription").value = "";
    $("nothing").checked = false;
    await refresh();
    showStatus("Entry added; other percentages were adjusted.");
  } catch (error) {
    showError(error.message);
  }
};

$("saveCollection").onclick = async () => {
  try {
    const key = $("locationSelect").value;
    const fish = $("collectionFish").value
      .split(/\r?\n/)
      .map(name => name.trim())
      .filter(Boolean);

    await api("/api/collections/" + encodeURIComponent(key), {
      method: "PATCH",
      body: {
        enabled: $("collectionEnabled").checked,
        fish,
        prompt: $("collectionPrompt").value
      }
    });
    await refresh();
    showStatus("Collection settings saved.");
  } catch (error) {
    showError(error.message);
  }
};

$("createLocation").onclick = async () => {
  try {
    await api("/api/locations", {
      method: "POST",
      body: {
        label: $("newLabel").value,
        rare_chance: $("newRare").value,
        common_fish: $("newCommon").value,
        rare_fish: $("newRareFish").value
      }
    });
    $("newLabel").value = "";
    $("newCommon").value = "";
    $("newRareFish").value = "";
    await refresh();
    showStatus("Location created.");
  } catch (error) {
    showError(error.message);
  }
};

$("uploadAvatar").onclick = async () => {
  try {
    const file = $("avatarFile").files[0];
    if (!file) throw new Error("Choose an image file first.");

    const form = new FormData();
    form.append("name", $("avatarName").value);
    form.append("image", file);

    const result = await api("/api/avatar", {method: "POST", body: form});
    await refresh();
    $("avatarLibrary").value = result.filename;
    $("fishName").value = $("avatarName").value.trim();
    showStatus(`Avatar saved locally as ${result.filename}.`);
  } catch (error) {
    showError(error.message);
  }
};

$("logout").onclick = () => {
  sessionStorage.removeItem("lokfishing-token");
  token = "";
  state = null;
  $("app").hidden = true;
  $("login").hidden = false;
  $("password").value = "";
  showError("");
};

if (token) {
  refresh().then(() => {
    $("login").hidden = true;
    $("app").hidden = false;
    showStatus("Connected.");
  }).catch(() => {
    token = "";
    sessionStorage.removeItem("lokfishing-token");
  });
}
</script>
</body>
</html>
"""