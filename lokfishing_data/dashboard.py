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
    "🎉 **{user}**, you completed the **{collection}** collection!"
)


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "fish"


def _location_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")


def _table_rates(table: list) -> list:
    """Return each entry's percentage of the table's total weight."""
    if not table:
        return []

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


def _percentage(value, label: str, allow_zero: bool = False) -> float:
    try:
        percent = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a valid percentage.") from exc

    valid_minimum = percent >= 0 if allow_zero else percent > 0
    if not math.isfinite(percent) or not valid_minimum or percent > 100:
        if allow_zero:
            raise ValueError(f"{label} must be between 0 and 100.")
        raise ValueError(f"{label} must be greater than 0 and at most 100.")

    return percent


def _positive_weight(value) -> float:
    try:
        weight = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Weight must be a positive number.") from exc

    if not math.isfinite(weight) or weight <= 0:
        raise ValueError("Weight must be a positive number.")

    return weight


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


def _clean_fish_list(value) -> list:
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
            raise ValueError("Nothing cannot be included in a collection.")

        normalized = name.casefold()
        if normalized not in seen:
            result.append(name)
            seen.add(normalized)

    if len(result) > MAX_COLLECTION_FISH:
        raise ValueError(
            f"A collection may contain at most {MAX_COLLECTION_FISH} fish."
        )
    return result


def _clean_prompt(value) -> str:
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

        self.app.router.add_patch("/api/game-settings", self.update_game_settings)

        self.app.router.add_post("/api/locations", self.create_location)
        self.app.router.add_patch("/api/locations/{key}", self.update_location)
        self.app.router.add_delete("/api/locations/{key}", self.delete_location)

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

        self.app.router.add_post("/api/collections", self.create_collection)
        self.app.router.add_patch(
            "/api/collections/{key}",
            self.update_collection,
        )
        self.app.router.add_delete(
            "/api/collections/{key}",
            self.delete_collection,
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
        """List local images, excluding symlinks."""
        if not AVATAR_DIR.is_dir():
            return []

        result = []
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

            result.append({
                "filename": path.name,
                "name": re.sub(
                    r"[-_]+",
                    " ",
                    path.stem,
                ).strip().title(),
            })

        return result

    async def get_state(self, request):
        """Return current settings, tables, collections, and image catalog."""
        locations = copy.deepcopy(await self.cog.config.locations())
        collections = copy.deepcopy(await self.cog.config.collections())
        descriptions = await self.cog.config.fish_descriptions()
        catalog = list(await self.cog.config.fish_catalog())
        avatars = self._list_avatars()

        catalog_keys = {str(name).casefold() for name in catalog}
        for avatar in avatars:
            if avatar["name"].casefold() not in catalog_keys:
                catalog.append(avatar["name"])
                catalog_keys.add(avatar["name"].casefold())

        avatar_by_slug = {
            _slug(Path(item["filename"]).stem): item["filename"]
            for item in avatars
        }

        for location in locations.values():
            for rarity in ("common", "rare"):
                table = location.get(rarity, [])
                rates = _table_rates(table)

                for entry, rate in zip(table, rates):
                    name = str(entry.get("name", "")).strip()
                    entry["rate_percent"] = rate

                    if name.casefold() == "nothing":
                        entry["avatar"] = None
                        entry["description"] = ""
                    else:
                        entry["avatar"] = avatar_by_slug.get(_slug(name))
                        entry["description"] = descriptions.get(
                            name.casefold(),
                            "",
                        )

        fish_catalog = [
            {
                "name": str(name),
                "avatar": avatar_by_slug.get(_slug(str(name))),
            }
            for name in sorted(catalog, key=lambda item: str(item).casefold())
        ]

        return web.json_response({
            "locations": locations,
            "collections": collections,
            "fish_catalog": fish_catalog,
            "avatars": avatars,
            "game_settings": {
                "casts_per_bait": await self.cog.config.casts_per_bait(),
                "bait_cooldown_seconds": (
                    await self.cog.config.bait_cooldown_seconds()
                ),
            },
        })

    async def get_avatar(self, request):
        """Serve only an image in the local avatar library."""
        filename = request.match_info["filename"]
        allowed = {item["filename"] for item in self._list_avatars()}

        if filename not in allowed:
            return web.json_response(
                {"error": "Avatar not found."},
                status=404,
            )

        return web.FileResponse(AVATAR_DIR / filename)

    async def update_game_settings(self, request):
        try:
            data = await request.json()
            casts = int(data.get("casts_per_bait"))
            cooldown = int(data.get("bait_cooldown_seconds"))

            if not 1 <= casts <= 100:
                raise ValueError("Casts per bait must be between 1 and 100.")
            if not 0 <= cooldown <= 86400:
                raise ValueError(
                    "Cooldown must be between 0 and 86400 seconds."
                )

            async with self.cog._state_lock:
                await self.cog.config.casts_per_bait.set(casts)
                await self.cog.config.bait_cooldown_seconds.set(cooldown)

            return web.json_response({
                "ok": True,
                "casts_per_bait": casts,
                "bait_cooldown_seconds": cooldown,
            })
        except (ValueError, TypeError, KeyError) as exc:
            return web.json_response({"error": str(exc)}, status=400)

    async def create_location(self, request):
        try:
            data = await request.json()
            label = str(data.get("label", "")).strip()
            common_name = str(data.get("common_fish", "")).strip()
            rare_name = str(data.get("rare_fish", "")).strip()
            rare_chance = _percentage(
                data.get("rare_chance", 5),
                "Rare catch chance",
                allow_zero=True,
            )

            if not label or len(label) > 80:
                raise ValueError("Location name must be 1–80 characters.")
            if not common_name or len(common_name) > 80:
                raise ValueError("Enter an initial common fish.")
            if not rare_name or len(rare_name) > 80:
                raise ValueError("Enter an initial rare fish.")
            if common_name.casefold() == "nothing":
                raise ValueError("The initial common entry must be a fish.")
            if rare_name.casefold() == "nothing":
                raise ValueError("The initial rare entry must be a fish.")

            key = _location_key(label)
            if not key:
                raise ValueError(
                    "Location name must contain letters or numbers."
                )

            async with self.cog._state_lock:
                locations = await self.cog.config.locations()
                collections = await self.cog.config.collections()
                catalog = list(await self.cog.config.fish_catalog())

                if key in locations:
                    raise ValueError("A location with that name already exists.")

                locations[key] = {
                    "label": label,
                    "rare_chance": rare_chance,
                    "common": [{"name": common_name, "weight": 1.0}],
                    "rare": [{"name": rare_name, "weight": 1.0}],
                }
                collections.setdefault(
                    key,
                    {
                        "name": f"{label} Collection",
                        "enabled": False,
                        "fish": [],
                        "prompt": DEFAULT_COLLECTION_PROMPT,
                    },
                )

                known = {str(name).casefold() for name in catalog}
                for name in (common_name, rare_name):
                    if name.casefold() not in known:
                        catalog.append(name)
                        known.add(name.casefold())

                await self.cog.config.locations.set(locations)
                await self.cog.config.collections.set(collections)
                await self.cog.config.fish_catalog.set(catalog)

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
                    location["rare_chance"] = _percentage(
                        data["rare_chance"],
                        "Rare catch chance",
                        allow_zero=True,
                    )

                await self.cog.config.locations.set(locations)

            return web.json_response({"ok": True})
        except (ValueError, TypeError, KeyError) as exc:
            return web.json_response({"error": str(exc)}, status=400)

    async def delete_location(self, request):
        key = request.match_info["key"]

        async with self.cog._state_lock:
            locations = await self.cog.config.locations()

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

            del locations[key]
            await self.cog.config.locations.set(locations)

        # Global fish collections are deliberately not tied to locations.
        return web.json_response({"ok": True})

    async def add_entry(self, request):
        """Add an entry with a within-table percentage."""
        key = request.match_info["key"]

        try:
            data = await request.json()
            rarity = str(data.get("rarity", "")).lower()
            name = str(data.get("name", "")).strip()
            rate = _percentage(
                data.get("rate_percent"),
                "Rarity percentage",
            )
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
                    raise ValueError("That entry already exists in this table.")
                if len(table) >= 100:
                    raise ValueError("This table already has 100 entries.")

                if table:
                    if rate >= 100:
                        raise ValueError(
                            "When adding to a non-empty table, the new rate "
                            "must be below 100%."
                        )

                    total = sum(float(entry["weight"]) for entry in table)
                    remaining_rate = 100.0 - rate

                    for entry in table:
                        existing_rate = (
                            float(entry["weight"]) * 100.0 / total
                        )
                        entry["weight"] = (
                            existing_rate * remaining_rate / 100.0
                        )
                else:
                    rate = 100.0

                new_entry = {"name": name, "weight": rate}
                if nothing:
                    new_entry["type"] = "nothing"
                table.append(new_entry)
                await self.cog.config.locations.set(locations)

                if not nothing:
                    # Update the catalog directly under the existing lock.
                    # Do not call add_catalog_names here; it acquires this lock.
                    catalog = list(await self.cog.config.fish_catalog())
                    if name.casefold() not in {
                        str(item).casefold() for item in catalog
                    }:
                        catalog.append(name)
                        await self.cog.config.fish_catalog.set(catalog)

                    if description:
                        descriptions = await self.cog.config.fish_descriptions()
                        descriptions[name.casefold()] = description
                        await self.cog.config.fish_descriptions.set(descriptions)

            return web.json_response({"ok": True})
        except (ValueError, TypeError, KeyError) as exc:
            return web.json_response({"error": str(exc)}, status=400)

    async def update_entry(self, request):
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
                            "An entry cannot be 100% while others remain."
                        )

                    total_others = sum(
                        float(entry["weight"]) for entry in others
                    )
                    if not math.isfinite(total_others) or total_others <= 0:
                        raise ValueError("Other table entries have invalid rates.")

                    remaining = 100.0 - new_rate
                    target["weight"] = new_rate
                    for entry in others:
                        entry["weight"] = (
                            float(entry["weight"]) * remaining / total_others
                        )

                await self.cog.config.locations.set(locations)

            return web.json_response({"ok": True})
        except (ValueError, TypeError, KeyError) as exc:
            return web.json_response({"error": str(exc)}, status=400)

    async def delete_entry(self, request):
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
                raise ValueError("Nothing cannot have a description.")

            async with self.cog._state_lock:
                descriptions = await self.cog.config.fish_descriptions()

                if description:
                    descriptions[name.casefold()] = description
                else:
                    descriptions.pop(name.casefold(), None)

                await self.cog.config.fish_descriptions.set(descriptions)

            return web.json_response({"ok": True})
        except (ValueError, TypeError, KeyError) as exc:
            return web.json_response({"error": str(exc)}, status=400)

    async def create_collection(self, request):
        try:
            data = await request.json()
            name = str(data.get("name", "")).strip()

            if not name or len(name) > 80:
                raise ValueError("Collection name must be 1–80 characters.")

            key = _location_key(name)
            if not key:
                raise ValueError(
                    "Collection name must contain letters or numbers."
                )

            async with self.cog._state_lock:
                collections = await self.cog.config.collections()
                if key in collections:
                    raise ValueError(
                        "A collection with that name already exists."
                    )

                collections[key] = {
                    "name": name,
                    "enabled": False,
                    "fish": [],
                    "prompt": DEFAULT_COLLECTION_PROMPT,
                }
                await self.cog.config.collections.set(collections)

            return web.json_response({"ok": True, "key": key})
        except (ValueError, TypeError, KeyError) as exc:
            return web.json_response({"error": str(exc)}, status=400)

    async def update_collection(self, request):
        key = request.match_info["key"]

        try:
            data = await request.json()
            name = str(data.get("name", "")).strip()
            enabled = data.get("enabled")
            fish = _clean_fish_list(data.get("fish", []))
            prompt = _clean_prompt(data.get("prompt", ""))

            if not name or len(name) > 80:
                raise ValueError("Collection name must be 1–80 characters.")
            if not isinstance(enabled, bool):
                raise ValueError("Enabled must be true or false.")
            if enabled and not fish:
                raise ValueError(
                    "An enabled collection needs at least one fish."
                )

            async with self.cog._state_lock:
                collections = await self.cog.config.collections()
                if key not in collections:
                    return web.json_response(
                        {"error": "Collection not found."},
                        status=404,
                    )

                collections[key] = {
                    "name": name,
                    "enabled": enabled,
                    "fish": fish,
                    "prompt": prompt,
                }
                await self.cog.config.collections.set(collections)

            return web.json_response({"ok": True})
        except (ValueError, TypeError, KeyError) as exc:
            return web.json_response({"error": str(exc)}, status=400)

    async def delete_collection(self, request):
        key = request.match_info["key"]

        async with self.cog._state_lock:
            collections = await self.cog.config.collections()
            if key not in collections:
                return web.json_response(
                    {"error": "Collection not found."},
                    status=404,
                )

            del collections[key]
            await self.cog.config.collections.set(collections)

        return web.json_response({"ok": True})

    async def upload_avatar(self, request):
        try:
            form = await request.post()
            fish_name = str(form.get("name", "")).strip()
            image_field = form.get("image")

            if not fish_name or len(fish_name) > 80:
                raise ValueError(
                    "Enter a fish name between 1 and 80 characters."
                )
            if not hasattr(image_field, "file"):
                raise ValueError("Choose an image file.")

            raw_image = image_field.file.read(MAX_IMAGE_BYTES + 1)
            if len(raw_image) > MAX_IMAGE_BYTES:
                raise ValueError("Image must be 10 MB or smaller.")
            if not raw_image:
                raise ValueError("Selected image is empty.")

            from PIL import Image

            with Image.open(io.BytesIO(raw_image)) as image:
                if image.size != (512, 512):
                    raise ValueError(
                        "Avatar image must be exactly 512×512 pixels."
                    )
                image.verify()

            with Image.open(io.BytesIO(raw_image)) as image:
                output = io.BytesIO()
                image.convert("RGBA").save(output, format="PNG")

            AVATAR_DIR.mkdir(parents=True, exist_ok=True)
            slug = _slug(fish_name)
            filename = f"{slug}.png"
            target = AVATAR_DIR / filename
            temporary = AVATAR_DIR / f".{slug}.tmp.png"

            temporary.write_bytes(output.getvalue())
            temporary.replace(target)

            await self.cog.add_catalog_names([fish_name])

            return web.json_response({
                "ok": True,
                "filename": filename,
            })
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
body{font:16px system-ui,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem;background:#101923;color:#edf4fa}
input,select,button,textarea{font:inherit;padding:.55rem;margin:.2rem;border-radius:6px;border:1px solid #526577}
button{cursor:pointer;background:#287bb5;color:white}
input,select,textarea{background:#182632;color:white}
textarea{box-sizing:border-box;width:100%;min-height:75px;resize:vertical}
section,.card{background:#17232e;padding:1rem;margin:1rem 0;border-radius:10px}
.row{display:flex;gap:.5rem;align-items:center;flex-wrap:wrap}
table{border-collapse:collapse;width:100%;margin:.5rem 0}
td,th{text-align:left;padding:.5rem;border-bottom:1px solid #40505e;vertical-align:top}
.small{color:#aebdca}
.danger{background:#a83d46}
.fish-icon{width:36px;height:36px;object-fit:cover;border-radius:6px;vertical-align:middle;margin-right:.5rem;background:#263746}
.description-editor{min-width:220px}
#error,#loginError{color:#ff9b9b;white-space:pre-wrap}
#status{color:#a9e6b0}
</style>
</head>
<body>
<h1>LokFishing Dashboard</h1>
<p class="small">Dashboard checks for changes every 5 seconds when you are not editing a field.</p>

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
<p class="small">Per-player across all locations. A Nothing result uses one cast.</p>
<div class="row">
<label>Casts per bait <input id="castsPerBait" type="number" min="1" max="100"></label>
<label>Reload cooldown, seconds <input id="baitCooldown" type="number" min="0" max="86400"></label>
<button id="saveBait">Save bait settings</button>
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
<button id="saveLocation">Save location</button>
<button id="deleteLocation" class="danger">Delete location</button>
</div>
</section>

<section>
<h2>Drop tables</h2>
<p class="small">Percentages are within each table. Editing one rate adjusts the others proportionally.</p>
<div id="tables"></div>
<h3>Add a fish to this location</h3>
<div class="row">
<select id="rarity"><option value="common">Common</option><option value="rare">Rare</option></select>
<select id="fishSelect"></select>
<input id="customFish" placeholder="Or type a fish name">
<label>Rarity % <input id="newRate" type="number" min="0.01" max="100" step="0.01" value="10"></label>
<label><input id="nothing" type="checkbox"> Nothing</label>
</div>
<label>Optional lore description</label>
<textarea id="newDescription" maxlength="1000" placeholder="Blank uses the basic catch message."></textarea>
<button id="addEntry">Add entry</button>
</section>

<section>
<h2>Global fish collections</h2>
<p class="small">A collection is independent of location; a player must own every listed fish.</p>
<div class="row">
<select id="collectionSelect"></select>
<input id="newCollectionName" placeholder="New collection name">
<button id="createCollection">Create collection</button>
<button id="deleteCollection" class="danger">Delete selected</button>
</div>
<div class="row">
<label>Collection name <input id="collectionName"></label>
<label><input id="collectionEnabled" type="checkbox"> Enabled</label>
<select id="collectionFishSelect"></select>
<button id="addCollectionFish">Add selected fish</button>
</div>
<label>Required fish, one per line</label>
<textarea id="collectionFish"></textarea>
<label>Completion prompt. Tokens: {user}, {collection}</label>
<textarea id="collectionPrompt" maxlength="1500"></textarea>
<button id="saveCollection">Save collection</button>
</section>

<section>
<h2>Upload avatar</h2>
<p class="small">Images stay in the local avatar folder and are not uploaded to GitHub.</p>
<div class="row">
<input id="avatarName" placeholder="Fish name">
<input id="avatarFile" type="file" accept="image/png,image/jpeg,image/webp">
<button id="uploadAvatar">Upload 512×512 avatar</button>
</div>
<h3>Fish catalog</h3>
<div id="catalogList"></div>
</section>

<section>
<h2>Add location</h2>
<div class="row">
<input id="newLocationLabel" placeholder="Location name">
<label>Rare catch chance %
  <input id="newLocationRare" type="number" min="0" max="100" step="0.1" value="5">
</label>
<select id="initialCommon"></select>
<select id="initialRare"></select>
<button id="createLocation">Create location</button>
</div>
</section>

<button id="logout" class="danger">Log out</button>
</main>

<script>
"use strict";

let token = sessionStorage.getItem("lokfishing-token") || "";
let state = null;
let stateSignature = "";
let refreshInProgress = false;
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
function makeNode(tag, text) {
  const element = document.createElement(tag);
  element.textContent = text;
  return element;
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

  if (!response.ok) {
    throw new Error(data.error || response.statusText);
  }
  return data;
}
function fillSelect(select, items, placeholder) {
  select.replaceChildren(new Option(placeholder, ""));
  for (const item of items) {
    select.add(new Option(item.name, item.name));
  }
}
async function loadImage(img, filename) {
  try {
    const response = await fetch(
      "/api/avatar/" + encodeURIComponent(filename),
      {headers: {Authorization: "Bearer " + token}}
    );
    if (!response.ok) return;

    const objectUrl = URL.createObjectURL(await response.blob());
    img.onload = () => URL.revokeObjectURL(objectUrl);
    img.onerror = () => URL.revokeObjectURL(objectUrl);
    img.src = objectUrl;
  } catch (error) {
    // Image preview failure should not prevent dashboard use.
  }
}
function hasFocusedEditor() {
  const active = document.activeElement;
  return active && active.matches("input, textarea, select");
}
function renderCatalog() {
  const root = $("catalogList");
  root.replaceChildren();

  for (const item of (state.fish_catalog || [])) {
    const line = document.createElement("div");

    if (item.avatar) {
      const img = document.createElement("img");
      img.className = "fish-icon";
      img.alt = item.name;
      line.append(img);
      loadImage(img, item.avatar);
    }

    line.append(document.createTextNode(item.name));
    root.append(line);
  }
}
function renderCollections() {
  const select = $("collectionSelect");
  const previous = select.value;
  select.replaceChildren(new Option("Select collection…", ""));

  for (const [key, collection] of Object.entries(state.collections || {})) {
    select.add(new Option(collection.name || key, key));
  }

  if (previous && state.collections[previous]) {
    select.value = previous;
  }

  fillSelect(
    $("collectionFishSelect"),
    state.fish_catalog || [],
    "Add fish from catalog…"
  );
  renderSelectedCollection();
}
function renderSelectedCollection() {
  const key = $("collectionSelect").value;
  const collection = (state.collections || {})[key];
  const enabled = Boolean(collection);

  for (const id of [
    "collectionName",
    "collectionEnabled",
    "collectionFish",
    "collectionPrompt",
    "saveCollection",
    "deleteCollection",
  ]) {
    $(id).disabled = !enabled;
  }

  if (!collection) {
    $("collectionName").value = "";
    $("collectionEnabled").checked = false;
    $("collectionFish").value = "";
    $("collectionPrompt").value = "";
    return;
  }

  $("collectionName").value = collection.name || key;
  $("collectionEnabled").checked = collection.enabled !== false;
  $("collectionFish").value = (collection.fish || []).join("\n");
  $("collectionPrompt").value = collection.prompt || "";
}
function renderLocation() {
  if (!state) return;

  const key = $("locationSelect").value;
  const location = state.locations[key];
  const root = $("tables");
  root.replaceChildren();

  if (!location) {
    root.append(makeNode("p", "Select a location."));
    return;
  }

  $("labelInput").value = location.label;
  $("rareInput").value = location.rare_chance;

  for (const rarity of ["common", "rare"]) {
    root.append(
      makeNode("h3", rarity[0].toUpperCase() + rarity.slice(1))
    );

    const table = document.createElement("table");
    const header = document.createElement("tr");

    for (const title of ["Fish", "Lore description", "Rarity %", "Actions"]) {
      header.append(makeNode("th", title));
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
        loadImage(img, entry.avatar);
      }

      nameCell.append(document.createTextNode(entry.name));
      row.append(nameCell);

      const descriptionCell = document.createElement("td");
      const description = document.createElement("textarea");
      description.maxLength = 1000;
      description.placeholder = "Optional lore; blank uses the basic catch message.";
      description.value = entry.description || "";

      const saveDescription = makeNode("button", "Save description");
      saveDescription.onclick = async () => {
        try {
          await api("/api/fish-description", {
            method: "PATCH",
            body: {
              name: entry.name,
              description: description.value
            }
          });
          await refreshState(true);
          showStatus("Description saved.");
        } catch (error) {
          showError(error.message);
        }
      };

      descriptionCell.append(description, saveDescription);
      row.append(descriptionCell);

      const rateCell = document.createElement("td");
      const rate = document.createElement("input");
      rate.type = "number";
      rate.min = "0.01";
      rate.max = "100";
      rate.step = "0.01";
      rate.value = Number(entry.rate_percent || 0).toFixed(2);
      rateCell.append(rate);
      row.append(rateCell);

      const actions = document.createElement("td");

      const saveRate = makeNode("button", "Save %");
      saveRate.onclick = async () => {
        try {
          const query = new URLSearchParams({key, rarity, name: entry.name});
          await api("/api/entry?" + query, {
            method: "PATCH",
            body: {rate_percent: rate.value}
          });
          await refreshState(true);
          showStatus("Rate saved; other entries were adjusted.");
        } catch (error) {
          showError(error.message);
        }
      };

      const remove = makeNode("button", "Remove");
      remove.className = "danger";
      remove.onclick = async () => {
        if (!confirm(`Remove ${entry.name} from the ${rarity} table?`)) return;

        try {
          const query = new URLSearchParams({key, rarity, name: entry.name});
          await api("/api/entry?" + query, {method: "DELETE"});
          await refreshState(true);
          showStatus("Entry removed; remaining rates now total 100%.");
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
function renderAll() {
  if (!state) return;

  $("castsPerBait").value = state.game_settings.casts_per_bait;
  $("baitCooldown").value = state.game_settings.bait_cooldown_seconds;

  const locationSelect = $("locationSelect");
  const previousLocation = locationSelect.value;
  locationSelect.replaceChildren();

  for (const [key, location] of Object.entries(state.locations)) {
    locationSelect.add(new Option(location.label, key));
  }
  if (previousLocation && state.locations[previousLocation]) {
    locationSelect.value = previousLocation;
  }

  fillSelect($("fishSelect"), state.fish_catalog || [], "Choose catalog fish…");
  fillSelect($("initialCommon"), state.fish_catalog || [], "Choose common fish…");
  fillSelect($("initialRare"), state.fish_catalog || [], "Choose rare fish…");

  renderCollections();
  renderCatalog();
  renderLocation();
}
async function refreshState(force = false) {
  if (refreshInProgress) return;
  if (!force && state && hasFocusedEditor()) return;

  refreshInProgress = true;
  try {
    const nextState = await api("/api/state");
    const nextSignature = JSON.stringify(nextState);

    if (!force && state && nextSignature === stateSignature) {
      return;
    }

    // While a field is focused, preserve its current contents. The next poll
    // will apply the latest server state after editing is no longer active.
    if (!force && state && hasFocusedEditor()) {
      return;
    }

    state = nextState;
    stateSignature = nextSignature;
    renderAll();
  } finally {
    refreshInProgress = false;
  }
}
async function runAction(action, successMessage) {
  try {
    await action();
    await refreshState(true);
    showStatus(successMessage);
  } catch (error) {
    showError(error.message);
  }
}

$("loginButton").onclick = async () => {
  token = $("password").value;
  try {
    await refreshState(true);
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
$("collectionSelect").onchange = renderSelectedCollection;

$("fishSelect").onchange = () => {
  if ($("fishSelect").value) {
    $("customFish").value = $("fishSelect").value;
  }
};

$("saveBait").onclick = () => runAction(
  () => api("/api/game-settings", {
    method: "PATCH",
    body: {
      casts_per_bait: $("castsPerBait").value,
      bait_cooldown_seconds: $("baitCooldown").value
    }
  }),
  "Bait settings saved."
);

$("saveLocation").onclick = () => runAction(
  () => api("/api/locations/" + encodeURIComponent($("locationSelect").value), {
    method: "PATCH",
    body: {
      label: $("labelInput").value,
      rare_chance: $("rareInput").value
    }
  }),
  "Location settings saved."
);

$("deleteLocation").onclick = async () => {
  const key = $("locationSelect").value;
  const location = state && state.locations[key];
  if (!location || !confirm(`Delete location "${location.label}"?`)) return;

  await runAction(
    () => api("/api/locations/" + encodeURIComponent(key), {
      method: "DELETE"
    }),
    "Location deleted."
  );
};

$("addEntry").onclick = async () => {
  const key = $("locationSelect").value;
  const name = $("customFish").value.trim() || $("fishSelect").value;

  await runAction(
    () => api("/api/locations/" + encodeURIComponent(key) + "/entries", {
      method: "POST",
      body: {
        rarity: $("rarity").value,
        name,
        rate_percent: $("newRate").value,
        description: $("newDescription").value,
        nothing: $("nothing").checked
      }
    }),
    "Entry added; other table rates were adjusted."
  );

  $("customFish").value = "";
  $("newDescription").value = "";
  $("nothing").checked = false;
};

$("createCollection").onclick = async () => {
  const name = $("newCollectionName").value.trim();
  try {
    const result = await api("/api/collections", {
      method: "POST",
      body: {name}
    });
    await refreshState(true);
    $("collectionSelect").value = result.key;
    renderSelectedCollection();
    $("newCollectionName").value = "";
    showStatus("Collection created. Add its fish and save it.");
  } catch (error) {
    showError(error.message);
  }
};

$("addCollectionFish").onclick = () => {
  const name = $("collectionFishSelect").value;
  if (!name) return;

  const fish = $("collectionFish").value
    .split(/\r?\n/)
    .map(value => value.trim())
    .filter(Boolean);

  if (!fish.some(value => value.toLowerCase() === name.toLowerCase())) {
    fish.push(name);
  }
  $("collectionFish").value = fish.join("\n");
};

$("saveCollection").onclick = async () => {
  const key = $("collectionSelect").value;
  const fish = $("collectionFish").value
    .split(/\r?\n/)
    .map(value => value.trim())
    .filter(Boolean);

  await runAction(
    () => api("/api/collections/" + encodeURIComponent(key), {
      method: "PATCH",
      body: {
        name: $("collectionName").value,
        enabled: $("collectionEnabled").checked,
        fish,
        prompt: $("collectionPrompt").value
      }
    }),
    "Global collection saved."
  );

  if (state && state.collections[key]) {
    $("collectionSelect").value = key;
    renderSelectedCollection();
  }
};

$("deleteCollection").onclick = async () => {
  const key = $("collectionSelect").value;
  if (!key || !confirm("Delete this global collection?")) return;

  await runAction(
    () => api("/api/collections/" + encodeURIComponent(key), {
      method: "DELETE"
    }),
    "Collection deleted."
  );
};

$("createLocation").onclick = () => runAction(
  () => api("/api/locations", {
    method: "POST",
    body: {
      label: $("newLocationLabel").value,
      rare_chance: $("newLocationRare").value,
      common_fish: $("initialCommon").value,
      rare_fish: $("initialRare").value
    }
  }),
  "Location created."
);

$("uploadAvatar").onclick = async () => {
  try {
    const file = $("avatarFile").files[0];
    if (!file) throw new Error("Choose an image first.");

    const form = new FormData();
    form.append("name", $("avatarName").value);
    form.append("image", file);

    await api("/api/avatar", {method: "POST", body: form});
    await refreshState(true);
    showStatus("Avatar uploaded to the local fish catalog.");
  } catch (error) {
    showError(error.message);
  }
};

$("logout").onclick = () => {
  sessionStorage.removeItem("lokfishing-token");
  token = "";
  state = null;
  stateSignature = "";
  $("app").hidden = true;
  $("login").hidden = false;
  $("password").value = "";
  showError("");
};

if (token) {
  refreshState(true)
    .then(() => {
      $("login").hidden = true;
      $("app").hidden = false;
      showStatus("Connected.");
    })
    .catch(() => {
      token = "";
      sessionStorage.removeItem("lokfishing-token");
    });
}

setInterval(() => {
  if (token && !$("app").hidden) {
    refreshState(false).catch(() => {});
  }
}, 5000);
</script>
</body>
</html>
"""