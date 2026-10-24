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
    """Convert a name to the lowercase, hyphenated form used for images."""
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "fish"


def _location_key(value: str) -> str:
    """Convert a location label into a simple configuration key."""
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")


def _positive_weight(value) -> float:
    try:
        weight = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Weight must be a positive number.") from exc

    if not math.isfinite(weight) or weight <= 0:
        raise ValueError("Weight must be a positive number.")

    return weight


def _percentage(value) -> float:
    try:
        percent = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Rare chance must be between 0 and 100.") from exc

    if not math.isfinite(percent) or not 0 <= percent <= 100:
        raise ValueError("Rare chance must be between 0 and 100.")

    return percent


def _clean_description(value) -> str:
    if value is None:
        return ""

    if not isinstance(value, str):
        raise ValueError("Description must be text.")

    result = value.strip()
    if len(result) > MAX_DESCRIPTION_LENGTH:
        raise ValueError(
            f"Description must be {MAX_DESCRIPTION_LENGTH} characters or fewer."
        )

    return result


def _clean_collection_fish(value) -> list:
    if not isinstance(value, list):
        raise ValueError("Collection fish must be a list of names.")

    fish = []
    seen = set()

    for raw_name in value:
        name = str(raw_name).strip()

        if not name:
            continue
        if len(name) > 80:
            raise ValueError("Fish names must be 80 characters or fewer.")
        if name.casefold() == "nothing":
            raise ValueError("Nothing cannot be a required collection fish.")

        key = name.casefold()
        if key not in seen:
            seen.add(key)
            fish.append(name)

    if len(fish) > MAX_COLLECTION_FISH:
        raise ValueError(
            f"A collection may contain at most {MAX_COLLECTION_FISH} fish."
        )

    return fish


def _clean_collection_prompt(value) -> str:
    if not isinstance(value, str):
        raise ValueError("Collection prompt must be text.")

    prompt = value.strip()
    if len(prompt) > MAX_COLLECTION_PROMPT_LENGTH:
        raise ValueError(
            f"Collection prompt must be "
            f"{MAX_COLLECTION_PROMPT_LENGTH} characters or fewer."
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
        """Require the dashboard password for every API endpoint."""
        if request.path.startswith("/api/"):
            authorization = request.headers.get("Authorization", "")
            supplied = authorization.removeprefix("Bearer ").strip()

            if not supplied or not hmac.compare_digest(supplied, self.password):
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

        result = []
        for path in sorted(AVATAR_DIR.iterdir(), key=lambda item: item.name.lower()):
            if (
                path.is_symlink()
                or not path.is_file()
                or path.suffix.lower() not in IMAGE_EXTENSIONS
            ):
                continue

            result.append(
                {
                    "filename": path.name,
                    "name": re.sub(r"[-_]+", " ", path.stem).strip().title(),
                }
            )

        return result

    async def get_state(self, request):
        """Return the current tables, descriptions, collections, and avatar list."""
        locations = copy.deepcopy(await self.cog.config.locations())
        descriptions = await self.cog.config.fish_descriptions()
        collections = copy.deepcopy(await self.cog.config.collections())
        avatars = self._list_avatars()

        avatar_by_slug = {
            _slug(Path(item["filename"]).stem): item["filename"]
            for item in avatars
        }

        for location in locations.values():
            for rarity in ("common", "rare"):
                for entry in location.get(rarity, []):
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

        return web.json_response(
            {
                "locations": locations,
                "collections": collections,
                "avatars": avatars,
            }
        )

    async def get_avatar(self, request):
        """Serve only an image that belongs to the local avatar library."""
        filename = request.match_info["filename"]
        allowed = {item["filename"] for item in self._list_avatars()}

        if filename not in allowed:
            return web.json_response(
                {"error": "Avatar not found."},
                status=404,
            )

        return web.FileResponse(AVATAR_DIR / filename)

    async def create_location(self, request):
        try:
            data = await request.json()
            label = str(data.get("label", "")).strip()
            common_name = str(data.get("common_fish", "")).strip()
            rare_name = str(data.get("rare_fish", "")).strip()
            rare_chance = _percentage(data.get("rare_chance", 5))

            if not label or len(label) > 80:
                raise ValueError("Location name must be 1–80 characters.")
            if not common_name or len(common_name) > 80:
                raise ValueError("Enter an initial common fish name.")
            if not rare_name or len(rare_name) > 80:
                raise ValueError("Enter an initial rare fish name.")
            if common_name.casefold() == "nothing":
                raise ValueError("The initial common fish must be a fish.")
            if rare_name.casefold() == "nothing":
                raise ValueError("The initial rare fish must be a fish.")

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
                    location["rare_chance"] = _percentage(data["rare_chance"])

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
        key = request.match_info["key"]

        try:
            data = await request.json()
            rarity = str(data.get("rarity", "")).lower()
            name = str(data.get("name", "")).strip()
            weight = _positive_weight(data.get("weight", 1))
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
                raise ValueError("Select the Nothing option for a no-catch entry.")

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

                entry = {"name": name, "weight": weight}
                if nothing:
                    entry["type"] = "nothing"
                table.append(entry)

                await self.cog.config.locations.set(locations)

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
            weight = _positive_weight(data.get("weight"))

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

                entry = next(
                    (
                        item
                        for item in location[rarity]
                        if str(item.get("name", "")).casefold()
                        == name.casefold()
                    ),
                    None,
                )

                if entry is None:
                    return web.json_response(
                        {"error": "Table entry not found."},
                        status=404,
                    )

                entry["weight"] = weight
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
            entry = next(
                (
                    item
                    for item in table
                    if str(item.get("name", "")).casefold() == name.casefold()
                ),
                None,
            )

            if entry is None:
                return web.json_response(
                    {"error": "Table entry not found."},
                    status=404,
                )
            if len(table) <= 1:
                return web.json_response(
                    {"error": "Keep at least one entry in each table."},
                    status=400,
                )

            table.remove(entry)
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

            return web.json_response(
                {"ok": True, "has_description": bool(description)}
            )

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
                    "An enabled collection must contain at least one fish."
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
  body {
    font: 16px system-ui, sans-serif;
    max-width: 1100px;
    margin: 2rem auto;
    padding: 0 1rem;
    background: #101923;
    color: #edf4fa;
  }
  input, select, button, textarea {
    font: inherit;
    padding: .55rem;
    margin: .2rem;
    border-radius: 6px;
    border: 1px solid #526577;
  }
  button { cursor: pointer; background: #287bb5; color: white; }
  input, select, textarea { background: #182632; color: white; }
  textarea { box-sizing: border-box; width: 100%; min-height: 75px; resize: vertical; }
  section, .card { background: #17232e; padding: 1rem; margin: 1rem 0; border-radius: 10px; }
  .row { display: flex; gap: .5rem; align-items: center; flex-wrap: wrap; }
  table { border-collapse: collapse; width: 100%; margin: .5rem 0; }
  td, th { text-align: left; padding: .5rem; border-bottom: 1px solid #40505e; vertical-align: top; }
  .small { color: #aebdca; }
  .danger { background: #a83d46; }
  .fish-icon { width: 36px; height: 36px; object-fit: cover; border-radius: 6px; vertical-align: middle; margin-right: .5rem; background: #263746; }
  .description-editor { min-width: 220px; }
  #error, #loginError { color: #ff9b9b; white-space: pre-wrap; }
  #status { color: #a9e6b0; }
</style>
</head>
<body>
  <h1>LokFishing Dashboard</h1>
  <p class="small">Manage locations, drop tables, fish descriptions, avatars, and collections.</p>

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
      <h2>Locations</h2>
      <div class="row">
        <select id="locationSelect"></select>
        <input id="labelInput" placeholder="Location label">
        <label>Rare chance %
          <input id="rareInput" type="number" min="0" max="100" step="0.1">
        </label>
        <button id="saveLocation">Save location settings</button>
        <button id="deleteLocation" class="danger">Delete location</button>
      </div>
    </section>

    <section>
      <h2>Drop tables</h2>
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
        <input id="weight" type="number" min="0.001" step="any" value="1">
        <label><input id="nothing" type="checkbox"> Nothing / no catch</label>
      </div>
      <label for="newFishDescription">Optional fish lore description</label>
      <textarea id="newFishDescription" maxlength="1000"
        placeholder="Leave blank for the basic catch message."></textarea>
      <button id="addEntry">Add entry</button>
      <p class="small">Weights are relative within each rarity table.</p>
    </section>

    <section>
      <h2>Collection for this location</h2>
      <label>
        <input id="collectionEnabled" type="checkbox">
        Enable this collection
      </label>
      <p class="small">
        Enter required fish one per line. By default this is the location's common fish.
        You can include any fish name, even if it is not currently in this location's table.
      </p>
      <label for="collectionFish">Required fish</label>
      <textarea id="collectionFish" placeholder="One fish name per line"></textarea>
      <p id="collectionSuggestions" class="small"></p>
      <label for="collectionPrompt">
        Completion prompt (tokens: {user}, {collection}, {location})
      </label>
      <textarea id="collectionPrompt" maxlength="1500"
        placeholder="🎉 {user}, you completed the {collection} collection at {location}!"></textarea>
      <button id="saveCollection">Save collection</button>
    </section>

    <section>
      <h2>Upload avatar</h2>
      <p class="small">
        Upload a square 512×512 image. It is saved in the server's local avatar folder,
        not GitHub.
      </p>
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
        <input id="newRare" type="number" min="0" max="100" step="0.1" value="5">
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
const $ = (id) => document.getElementById(id);

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

  if (!response.ok) {
    throw new Error(data.error || response.statusText);
  }
  return data;
}

function el(tag, value) {
  const node = document.createElement(tag);
  node.textContent = value;
  return node;
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
  } catch (error) {
    // A failed preview should not prevent dashboard use.
  }
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

  const select = $("locationSelect");
  const previousKey = select.value;
  select.replaceChildren();

  for (const [key, location] of Object.entries(state.locations)) {
    select.add(new Option(location.label, key));
  }

  if (previousKey && state.locations[previousKey]) {
    select.value = previousKey;
  }

  populateAvatarLibrary();
  renderLocation();
}

function renderCollection(key, location) {
  const collection = collectionFor(key, location);

  $("collectionEnabled").checked = collection.enabled !== false;
  $("collectionFish").value = (collection.fish || []).join("\n");
  $("collectionPrompt").value = collection.prompt || "";

  const candidates = [];
  const seen = new Set();

  for (const rarity of ["common", "rare"]) {
    for (const entry of (location[rarity] || [])) {
      if (entry.name.toLowerCase() === "nothing") continue;
      const normalized = entry.name.toLowerCase();
      if (!seen.has(normalized)) {
        seen.add(normalized);
        candidates.push(entry.name);
      }
    }
  }

  $("collectionSuggestions").textContent = candidates.length
    ? "Fish currently in this location's tables: " + candidates.join(", ")
    : "No fish are currently in this location's tables.";
}

function renderLocation() {
  if (!state) return;

  const key = $("locationSelect").value;
  const location = state.locations[key];

  if (!location) {
    $("tables").replaceChildren(el("p", "Select a location."));
    return;
  }

  $("labelInput").value = location.label;
  $("rareInput").value = location.rare_chance;
  renderCollection(key, location);

  const root = $("tables");
  root.replaceChildren();

  for (const rarity of ["common", "rare"]) {
    root.append(el("h3", rarity[0].toUpperCase() + rarity.slice(1)));

    const table = document.createElement("table");
    const header = document.createElement("tr");
    for (const title of ["Fish / outcome", "Lore description", "Weight", "Actions"]) {
      header.append(el("th", title));
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

      const saveDescription = el("button", "Save description");
      saveDescription.onclick = async () => {
        try {
          await api("/api/fish-description", {
            method: "PATCH",
            body: {name: entry.name, description: descriptionInput.value}
          });
          await refresh();
          showStatus(descriptionInput.value.trim()
            ? "Fish description saved."
            : "Description cleared; basic catch message will be used.");
        } catch (error) {
          showError(error.message);
        }
      };
      descriptionCell.append(descriptionInput, saveDescription);
      row.append(descriptionCell);

      const weightCell = document.createElement("td");
      const weightInput = document.createElement("input");
      weightInput.type = "number";
      weightInput.min = "0.001";
      weightInput.step = "any";
      weightInput.value = entry.weight;
      weightCell.append(weightInput);
      row.append(weightCell);

      const actions = document.createElement("td");

      const saveWeight = el("button", "Save weight");
      saveWeight.onclick = async () => {
        try {
          const query = new URLSearchParams({
            key, rarity, name: entry.name
          });
          await api("/api/entry?" + query, {
            method: "PATCH",
            body: {weight: weightInput.value}
          });
          await refresh();
          showStatus("Weight saved.");
        } catch (error) {
          showError(error.message);
        }
      };

      const remove = el("button", "Remove");
      remove.className = "danger";
      remove.onclick = async () => {
        if (!confirm(`Remove ${entry.name} from the ${rarity} table?`)) return;

        try {
          const query = new URLSearchParams({
            key, rarity, name: entry.name
          });
          await api("/api/entry?" + query, {method: "DELETE"});
          await refresh();
          showStatus("Entry removed.");
        } catch (error) {
          showError(error.message);
        }
      };

      actions.append(saveWeight, remove);
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

$("saveLocation").onclick = async () => {
  try {
    const key = $("locationSelect").value;
    await api("/api/locations/" + encodeURIComponent(key), {
      method: "PATCH",
      body: {
        label: $("labelInput").value,
        rare_chance: $("rareInput").value
      }
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
        weight: $("weight").value,
        description: $("newFishDescription").value,
        nothing: $("nothing").checked
      }
    });

    $("fishName").value = "";
    $("newFishDescription").value = "";
    $("nothing").checked = false;
    await refresh();
    showStatus("Drop-table entry added.");
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

    const result = await api("/api/avatar", {
      method: "POST",
      body: form
    });

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
  refresh()
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
</script>
</body>
</html>
"""