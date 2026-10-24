import hmac
import io
import math
import re
from pathlib import Path

from aiohttp import web


AVATAR_DIR = Path("/home/cogs/lokfishing/avatars")
MAX_IMAGE_BYTES = 10 * 1024 * 1024


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "fish"


def _location_key(value: str) -> str:
    return re.sub(r"[\s-]+", "_", value.strip().lower())


def _positive_weight(value) -> float:
    weight = float(value)
    if not math.isfinite(weight) or weight <= 0:
        raise ValueError("Weight must be a positive number.")
    return weight


def _percentage(value) -> float:
    percent = float(value)
    if not math.isfinite(percent) or not 0 <= percent <= 100:
        raise ValueError("Rare chance must be between 0 and 100.")
    return percent


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
        self.app.router.add_post("/api/avatar", self.upload_avatar)

    @web.middleware
    async def _auth_middleware(self, request, handler):
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
        site = web.TCPSite(self.runner, host="0.0.0.0", port=self.port)
        await site.start()

    async def close(self):
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None

    async def index(self, request):
        return web.Response(text=DASHBOARD_HTML, content_type="text/html")

    async def get_state(self, request):
        locations = await self.cog.config.locations()
        return web.json_response({"locations": locations})

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
                raise ValueError("Use a fish name, not Nothing, for the initial fish.")
            if rare_name.casefold() == "nothing":
                raise ValueError("Use a fish name, not Nothing, for the initial fish.")

            key = _location_key(label)
            if not key or not re.fullmatch(r"[a-z0-9_]+", key):
                raise ValueError("That location name cannot be used as a key.")

            async with self.cog._state_lock:
                locations = await self.cog.config.locations()
                if key in locations:
                    raise ValueError("A location with that name already exists.")

                locations[key] = {
                    "label": label,
                    "rare_chance": rare_chance,
                    "common": [{"name": common_name, "weight": 1.0}],
                    "rare": [{"name": rare_name, "weight": 1.0}],
                }
                await self.cog.config.locations.set(locations)

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
                        raise ValueError("Location name must be 1–80 characters.")
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
            await self.cog.config.locations.set(locations)

        return web.json_response({"ok": True, "deleted": label})

    async def add_entry(self, request):
        key = request.match_info["key"]

        try:
            data = await request.json()
            rarity = str(data.get("rarity", "")).lower()
            name = str(data.get("name", "")).strip()
            weight = _positive_weight(data.get("weight", 1))
            is_nothing = bool(data.get("nothing", False))

            if rarity not in {"common", "rare"}:
                raise ValueError("Rarity must be common or rare.")
            if is_nothing:
                name = "Nothing"
            elif not name or len(name) > 80:
                raise ValueError("Fish name must be 1–80 characters.")
            elif name.casefold() == "nothing":
                raise ValueError("Use the Nothing option for a no-catch entry.")

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
                if is_nothing:
                    entry["type"] = "nothing"
                table.append(entry)

                await self.cog.config.locations.set(locations)

            return web.json_response({"ok": True})

        except (ValueError, TypeError, KeyError) as exc:
            return web.json_response({"error": str(exc)}, status=400)

    async def update_entry(self, request):
        key = request.query.get("key", "")
        rarity = request.query.get("rarity", "").lower()
        name = request.query.get("name", "")

        try:
            weight = _positive_weight((await request.json()).get("weight"))

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
                        if item.get("name", "").casefold() == name.casefold()
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
                    if item.get("name", "").casefold() == name.casefold()
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

    async def upload_avatar(self, request):
        try:
            form = await request.post()
            fish_name = str(form.get("name", "")).strip()
            image_field = form.get("image")

            if not fish_name or len(fish_name) > 80:
                raise ValueError("Enter a fish name between 1 and 80 characters.")
            if not hasattr(image_field, "file"):
                raise ValueError("Choose an image file.")

            raw = image_field.file.read(MAX_IMAGE_BYTES + 1)
            if len(raw) > MAX_IMAGE_BYTES:
                raise ValueError("Image must be 10 MB or smaller.")

            from PIL import Image

            with Image.open(io.BytesIO(raw)) as image:
                if image.size != (512, 512):
                    raise ValueError("Avatar image must be exactly 512×512 pixels.")
                image.verify()

            # Normalize uploaded files to 512x512 PNG, regardless of source format.
            with Image.open(io.BytesIO(raw)) as image:
                image = image.convert("RGBA")
                output = io.BytesIO()
                image.save(output, format="PNG")

            AVATAR_DIR.mkdir(parents=True, exist_ok=True)
            slug = _slug(fish_name)
            target = AVATAR_DIR / f"{slug}.png"
            temporary = AVATAR_DIR / f".{slug}.tmp.png"
            temporary.write_bytes(output.getvalue())
            temporary.replace(target)

            return web.json_response(
                {"ok": True, "filename": target.name}
            )

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
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>LokFishing Dashboard</title>
<style>
body{font:16px system-ui,sans-serif;max-width:1000px;margin:2rem auto;padding:0 1rem;background:#101923;color:#edf4fa}
input,select,button{font:inherit;padding:.55rem;margin:.2rem;border-radius:6px;border:1px solid #526577}
button{cursor:pointer;background:#287bb5;color:white}
input,select{background:#182632;color:#fff}
section,.card{background:#17232e;padding:1rem;margin:1rem 0;border-radius:10px}
.row{display:flex;gap:.5rem;align-items:center;flex-wrap:wrap}
table{border-collapse:collapse;width:100%;margin:.5rem 0}
td,th{text-align:left;padding:.5rem;border-bottom:1px solid #40505e}
.small{color:#aebdca}.danger{background:#a83d46}
#error{color:#ff9b9b;white-space:pre-wrap}
</style>
</head>
<body>
<h1>LokFishing Dashboard</h1>
<p class="small">Manage locations, weighted tables, and local fish avatars.</p>
<div id="login" class="card">
  <label>Dashboard password <input id="password" type="password" autocomplete="current-password"></label>
  <button id="loginButton">Connect</button>
</div>
<div id="app" hidden>
  <div id="error"></div>
  <section>
    <h2>Locations</h2>
    <div class="row">
      <select id="locationSelect"></select>
      <input id="labelInput" placeholder="Location label">
      <label>Rare chance % <input id="rareInput" type="number" min="0" max="100" step="0.1"></label>
      <button id="saveLocation">Save location settings</button>
      <button id="deleteLocation" class="danger">Delete location</button>
    </div>
  </section>
  <section>
    <h2>Drop tables</h2>
    <div id="tables"></div>
    <h3>Add table entry</h3>
    <div class="row">
      <select id="rarity"><option>common</option><option>rare</option></select>
      <input id="fishName" placeholder="Fish name">
      <input id="weight" type="number" min="0.001" step="any" value="1" placeholder="Weight">
      <label><input id="nothing" type="checkbox"> Nothing / no catch</label>
      <button id="addEntry">Add entry</button>
    </div>
  </section>
  <section>
    <h2>Upload avatar</h2>
    <p class="small">Choose the fish name used in a table and a square 512×512 image.</p>
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
      <input id="newRare" type="number" min="0" max="100" step="0.1" value="5" placeholder="Rare chance %">
      <input id="newCommon" placeholder="Initial common fish">
      <input id="newRareFish" placeholder="Initial rare fish">
      <button id="createLocation">Create location</button>
    </div>
  </section>
  <button id="logout" class="danger">Log out</button>
</div>
<script>
let token = sessionStorage.getItem("lokfishing-token") || "";
let state = null;
const $ = id => document.getElementById(id);
function error(message=""){ $("error").textContent=message; }
async function api(url, options={}) {
  const headers = new Headers(options.headers || {});
  headers.set("Authorization", "Bearer " + token);
  let body = options.body;
  if (body && !(body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
    body = JSON.stringify(body);
  }
  const response = await fetch(url, {...options, headers, body});
  const data = await response.json().catch(()=>({error:response.statusText}));
  if (!response.ok) throw new Error(data.error || response.statusText);
  return data;
}
function text(tag, value) {
  const el = document.createElement(tag);
  el.textContent = value;
  return el;
}
async function refresh() {
  state = await api("/api/state");
  $("locationSelect").replaceChildren();
  for (const [key, loc] of Object.entries(state.locations)) {
    const option = document.createElement("option");
    option.value = key;
    option.textContent = loc.label;
    $("locationSelect").append(option);
  }
  renderLocation();
}
function renderLocation() {
  if (!state) return;
  const key = $("locationSelect").value;
  const loc = state.locations[key];
  if (!loc) return;
  $("labelInput").value = loc.label;
  $("rareInput").value = loc.rare_chance;
  const root = $("tables");
  root.replaceChildren();

  for (const rarity of ["common", "rare"]) {
    const heading = text("h3", rarity[0].toUpperCase()+rarity.slice(1));
    root.append(heading);
    const table = document.createElement("table");
    const header = document.createElement("tr");
    for (const title of ["Outcome", "Weight", "Actions"]) header.append(text("th", title));
    table.append(header);

    for (const entry of loc[rarity] || []) {
      const row = document.createElement("tr");
      row.append(text("td", entry.name));
      const weightCell = document.createElement("td");
      const weight = document.createElement("input");
      weight.type = "number"; weight.min = "0.001"; weight.step = "any";
      weight.value = entry.weight;
      weightCell.append(weight);
      row.append(weightCell);
      const actions = document.createElement("td");
      const save = text("button", "Save weight");
      save.onclick = async () => {
        try {
          const q = new URLSearchParams({key, rarity, name:entry.name});
          await api("/api/entry?"+q, {method:"PATCH", body:{weight:weight.value}});
          await refresh(); error("Saved.");
        } catch(e) { error(e.message); }
      };
      const remove = text("button", "Remove");
      remove.className = "danger";
      remove.onclick = async () => {
        try {
          const q = new URLSearchParams({key, rarity, name:entry.name});
          await api("/api/entry?"+q, {method:"DELETE"});
          await refresh(); error("Removed.");
        } catch(e) { error(e.message); }
      };
      actions.append(save, remove);
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
    $("login").hidden = true; $("app").hidden = false; error("");
  } catch(e) { token=""; error(e.message); }
};
$("locationSelect").onchange = renderLocation;
$("saveLocation").onclick = async () => {
  try {
    const key=$("locationSelect").value;
    await api("/api/locations/"+encodeURIComponent(key), {
      method:"PATCH",
      body:{label:$("labelInput").value, rare_chance:$("rareInput").value}
    });
    await refresh(); error("Location settings saved.");
  } catch(e) { error(e.message); }
};
$("deleteLocation").onclick = async () => {
  const key=$("locationSelect").value;
  if (!confirm("Delete this location and its tables?")) return;
  try {
    await api("/api/locations/"+encodeURIComponent(key), {method:"DELETE"});
    await refresh(); error("Location deleted.");
  } catch(e) { error(e.message); }
};
$("addEntry").onclick = async () => {
  try {
    const key=$("locationSelect").value;
    await api("/api/locations/"+encodeURIComponent(key)+"/entries", {
      method:"POST",
      body:{
        rarity:$("rarity").value,
        name:$("fishName").value,
        weight:$("weight").value,
        nothing:$("nothing").checked
      }
    });
    $("fishName").value=""; $("nothing").checked=false;
    await refresh(); error("Entry added.");
  } catch(e) { error(e.message); }
};
$("createLocation").onclick = async () => {
  try {
    await api("/api/locations", {
      method:"POST",
      body:{
        label:$("newLabel").value,
        rare_chance:$("newRare").value,
        common_fish:$("newCommon").value,
        rare_fish:$("newRareFish").value
      }
    });
    $("newLabel").value=""; $("newCommon").value=""; $("newRareFish").value="";
    await refresh(); error("Location created.");
  } catch(e) { error(e.message); }
};
$("uploadAvatar").onclick = async () => {
  try {
    const form = new FormData();
    form.append("name", $("avatarName").value);
    form.append("image", $("avatarFile").files[0]);
    await api("/api/avatar", {method:"POST", body:form});
    error("Avatar uploaded to the local avatars folder.");
  } catch(e) { error(e.message); }
};
$("logout").onclick = () => {
  sessionStorage.removeItem("lokfishing-token");
  token=""; state=null; $("app").hidden=true; $("login").hidden=false;
  $("password").value=""; error("Logged out.");
};
if (token) {
  refresh().then(()=>{$("login").hidden=true;$("app").hidden=false;})
    .catch(()=>{token="";sessionStorage.removeItem("lokfishing-token");});
}
</script>
</body>
</html>
"""