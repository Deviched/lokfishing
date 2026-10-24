import asyncio
import hashlib
import io
import json
import logging
import math
import os
import random
import re
import time
from pathlib import Path

import discord
from redbot.core import Config, checks, commands

from .dashboard import DEFAULT_COLLECTION_PROMPT, FishingDashboard


log = logging.getLogger("red.lokfishing")

AVATAR_DIR = Path("/home/cogs/lokfishing/avatars")
ULTRARARE_THRESHOLD_PERCENT = 5.0
MAX_MENU_LOCATIONS = 25
MAX_FISH_DESCRIPTION_LENGTH = 1000
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")
RARITIES = {"common", "rare"}


def fish_slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "fish"


def normalize_location(value: str) -> str:
    return re.sub(r"[\s-]+", "_", value.strip().lower())


def is_nothing(entry: dict) -> bool:
    return (
        entry.get("type") == "nothing"
        or str(entry.get("name", "")).strip().casefold() == "nothing"
    )


def load_default_data():
    """Load the bundled location tables and independent fish catalog."""
    path = Path(__file__).with_name("loot_tables.json")

    try:
        with path.open("r", encoding="utf-8") as data_file:
            raw = json.load(data_file)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not load {path}: {exc}") from exc

    # Also accept the older format where the JSON root was the locations map.
    if isinstance(raw, dict) and "locations" in raw:
        locations = raw["locations"]
        catalog = raw.get("fish_catalog", [])
    else:
        locations = raw
        catalog = []

    if not isinstance(locations, dict) or not locations:
        raise RuntimeError("loot_tables.json must contain non-empty locations.")
    if not isinstance(catalog, list):
        raise RuntimeError("fish_catalog must be a list of fish names.")

    cleaned_catalog = []
    seen_catalog = set()
    for item in catalog:
        name = str(item).strip()
        if name and name.casefold() not in seen_catalog:
            cleaned_catalog.append(name)
            seen_catalog.add(name.casefold())

    for key, location in locations.items():
        if not isinstance(key, str) or not re.fullmatch(r"[a-z0-9_]+", key):
            raise RuntimeError(f"Invalid location key: {key!r}")
        if not isinstance(location, dict):
            raise RuntimeError(f"Location {key!r} must be an object.")
        if not isinstance(location.get("label"), str) or not location["label"].strip():
            raise RuntimeError(f"Location {key!r} needs a label.")

        try:
            chance = float(location["rare_chance"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"Location {key!r} needs rare_chance.") from exc

        if not math.isfinite(chance) or not 0 <= chance <= 100:
            raise RuntimeError(f"Invalid rare_chance for {key!r}.")

        for rarity in RARITIES:
            table = location.get(rarity)
            if not isinstance(table, list) or not table:
                raise RuntimeError(
                    f"Location {key!r} needs a non-empty {rarity} table."
                )
            for entry in table:
                if not isinstance(entry, dict) or not str(entry.get("name", "")).strip():
                    raise RuntimeError(f"Invalid entry in {key!r}/{rarity}.")
                try:
                    weight = float(entry.get("weight", 0))
                except (TypeError, ValueError) as exc:
                    raise RuntimeError("Invalid table weight.") from exc
                if not math.isfinite(weight) or weight <= 0:
                    raise RuntimeError("Table weights must be positive.")

    return locations, cleaned_catalog


DEFAULT_LOCATIONS, DEFAULT_FISH_CATALOG = load_default_data()


class LocationButton(discord.ui.Button):
    def __init__(self, cog, key: str, label: str):
        super().__init__(
            label=label,
            style=discord.ButtonStyle.primary,
            custom_id=f"lokfishing:{key}",
        )
        self.cog = cog
        self.key = key

    async def callback(self, interaction: discord.Interaction):
        await self.cog.handle_catch(interaction, self.key)


class FishingMenu(discord.ui.View):
    def __init__(self, cog, locations: dict):
        super().__init__(timeout=900)
        self.message = None

        for key, location in list(locations.items())[:MAX_MENU_LOCATIONS]:
            self.add_item(LocationButton(cog, key, location["label"]))

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True

        if self.message:
            try:
                await self.message.edit(view=self)
            except (
                discord.NotFound,
                discord.Forbidden,
                discord.HTTPException,
            ):
                pass


class LokFishing(commands.Cog):
    """Fishing game with global collections, bait, and local dashboard."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(
            self,
            identifier=0x4C4F4B46495348,
            force_registration=True,
        )
        self.config.register_global(
            locations=DEFAULT_LOCATIONS,
            fish_catalog=DEFAULT_FISH_CATALOG,
            fish_descriptions={},
            collections={},
            casts_per_bait=6,
            bait_cooldown_seconds=30,
            cooldown_seconds=30,
        )
        self.config.register_user(
            inventory={},
            bait_state={},
            completed_collections={},
        )

        self._state_lock = asyncio.Lock()
        self._dashboard = None

    async def add_catalog_names(self, names):
        """Add fish names to the dashboard catalog without adding drop entries."""
        async with self._state_lock:
            current = list(await self.config.fish_catalog())
            seen = {str(name).casefold() for name in current}

            for raw_name in names:
                name = str(raw_name).strip()
                if name and name.casefold() not in seen:
                    current.append(name)
                    seen.add(name.casefold())

            await self.config.fish_catalog.set(current)

    async def _migrate_old_collections(self):
        """Convert old location-shaped collection settings to global sets."""
        async with self._state_lock:
            locations = await self.config.locations()
            collections = await self.config.collections()
            changed = False

            for key, value in list(collections.items()):
                if not isinstance(value, dict) or "name" in value:
                    continue
                if "fish" not in value:
                    continue

                label = locations.get(key, {}).get("label", key.replace("_", " ").title())
                new_key = f"legacy_{key}"
                suffix = 2
                while new_key in collections and new_key != key:
                    new_key = f"legacy_{key}_{suffix}"
                    suffix += 1

                collections[new_key] = {
                    "name": f"{label} Collection",
                    "enabled": bool(value.get("enabled", True)),
                    "fish": list(value.get("fish", [])),
                    "prompt": str(value.get("prompt", DEFAULT_COLLECTION_PROMPT)),
                }
                del collections[key]
                changed = True

            if changed:
                await self.config.collections.set(collections)

    async def _seed_catalog(self):
        await self.add_catalog_names(DEFAULT_FISH_CATALOG)

    async def cog_load(self):
        await self._migrate_old_collections()
        await self._seed_catalog()

        password = os.getenv("LOKFISHING_DASHBOARD_PASSWORD")
        if not password:
            log.info("LokFishing dashboard disabled: password is not configured.")
            return
        if len(password) < 24:
            log.error("Dashboard password must be at least 24 characters.")
            return

        try:
            port = int(os.getenv("LOKFISHING_DASHBOARD_PORT", "8788"))
            self._dashboard = FishingDashboard(self, password, port)
            await self._dashboard.start()
            log.info("LokFishing dashboard started on port %s.", port)
        except Exception:
            self._dashboard = None
            log.exception("Could not start LokFishing dashboard.")

    async def cog_unload(self):
        if self._dashboard is not None:
            await self._dashboard.close()
            self._dashboard = None

    @commands.group(name="lokfish", invoke_without_command=True)
    async def lokfish(self, ctx):
        """Open the fishing location menu."""
        locations = await self.config.locations()
        if not locations:
            await ctx.send("No fishing locations are configured.")
            return

        view = FishingMenu(self, locations)
        message = await ctx.send(
            "**Legend of Korra Fishing**\nChoose a location to cast your line:",
            view=view,
        )
        view.message = message

    @staticmethod
    def _validated_table(location, rarity):
        table = location.get(rarity)
        if not isinstance(table, list) or not table:
            raise ValueError(f"The {rarity} table is empty or invalid.")

        weights = []
        for entry in table:
            if not isinstance(entry, dict) or not str(entry.get("name", "")).strip():
                raise ValueError(f"The {rarity} table contains an invalid entry.")
            weight = float(entry.get("weight", 0))
            if not math.isfinite(weight) or weight <= 0:
                raise ValueError(f"The {rarity} table contains an invalid weight.")
            weights.append(weight)

        return table, weights

    def _fish_chance_percent(self, location, rare_chance, fish_name):
        result = 0.0
        for rarity, branch_chance in (
            ("common", 100.0 - rare_chance),
            ("rare", rare_chance),
        ):
            if branch_chance <= 0:
                continue

            table, weights = self._validated_table(location, rarity)
            total = sum(weights)
            for entry, weight in zip(table, weights):
                if (
                    not is_nothing(entry)
                    and entry["name"].casefold() == fish_name.casefold()
                ):
                    result += branch_chance * weight / total

        return min(result, 100.0)

    async def _newly_completed_collections(self, user, inventory):
        collections = await self.config.collections()
        user_config = self.config.user(user)
        completed = await user_config.completed_collections()

        if not isinstance(completed, dict):
            completed = {}

        owned = {
            str(name).casefold()
            for name, count in inventory.items()
            if int(count) > 0
        }

        prompts = []
        changed = False

        for key, collection in collections.items():
            if not isinstance(collection, dict) or not collection.get("enabled", True):
                continue

            required = collection.get("fish", [])
            if not isinstance(required, list) or not required:
                continue

            required_keys = sorted({
                str(name).strip().casefold()
                for name in required
                if str(name).strip()
            })
            if not required_keys or not all(name in owned for name in required_keys):
                continue

            signature = hashlib.sha256(
                json.dumps(required_keys, separators=(",", ":")).encode("utf-8")
            ).hexdigest()

            if completed.get(key) == signature:
                continue

            completed[key] = signature
            changed = True

            collection_name = str(collection.get("name", key))
            prompt = str(collection.get("prompt", "")).strip()
            if not prompt:
                prompt = DEFAULT_COLLECTION_PROMPT

            prompt = (
                prompt.replace("{user}", user.display_name)
                .replace("{collection}", collection_name)
            )
            prompts.append(prompt[:1900])

        if changed:
            await user_config.completed_collections.set(completed)

        return prompts

    async def handle_catch(self, interaction, location_key):
        if interaction.user.bot:
            await interaction.response.send_message("Bots can't fish.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        cooldown_message = None
        config_error = False
        no_catch = False
        location_missing = False
        fish_name = None
        fish_description = ""
        rarity = None
        amount = 0
        fish_chance = None
        location = None
        bait_status = ""
        completion_prompts = []

        async with self._state_lock:
            locations = await self.config.locations()
            location = locations.get(location_key)

            if location is None:
                location_missing = True
            else:
                try:
                    casts_per_bait = int(await self.config.casts_per_bait())
                    cooldown = int(await self.config.bait_cooldown_seconds())
                    if not 1 <= casts_per_bait <= 100:
                        raise ValueError("Invalid casts-per-bait setting.")
                    if not 0 <= cooldown <= 86400:
                        raise ValueError("Invalid bait cooldown setting.")

                    user_config = self.config.user(interaction.user)
                    bait_state = await user_config.bait_state()
                    if not isinstance(bait_state, dict):
                        bait_state = {}

                    now = time.time()
                    remaining_casts = int(
                        bait_state.get("casts_remaining", casts_per_bait)
                    )
                    cooldown_until = float(bait_state.get("cooldown_until", 0))

                    if cooldown_until > now:
                        wait = math.ceil(cooldown_until - now)
                        cooldown_message = (
                            f"Your bait is reloading. Try again in **{wait}** "
                            f"second{'s' if wait != 1 else ''}."
                        )
                    else:
                        if cooldown_until or remaining_casts <= 0:
                            remaining_casts = casts_per_bait
                            cooldown_until = 0
                        remaining_casts = min(remaining_casts, casts_per_bait)

                        rare_chance = float(location["rare_chance"])
                        if not math.isfinite(rare_chance) or not 0 <= rare_chance <= 100:
                            raise ValueError("Rare chance must be between 0 and 100.")

                        rarity = random.choices(
                            ["rare", "common"],
                            weights=[rare_chance, 100.0 - rare_chance],
                            k=1,
                        )[0]

                        table, weights = self._validated_table(location, rarity)
                        outcome = random.choices(table, weights=weights, k=1)[0]
                        no_catch = is_nothing(outcome)

                        if not no_catch:
                            fish_name = outcome["name"]
                            fish_chance = self._fish_chance_percent(
                                location,
                                rare_chance,
                                fish_name,
                            )
                            amount = 1 if rarity == "rare" else random.randint(1, 3)

                            descriptions = await self.config.fish_descriptions()
                            fish_description = str(
                                descriptions.get(fish_name.casefold(), "")
                            ).strip()[:MAX_FISH_DESCRIPTION_LENGTH]

                            inventory = await user_config.inventory()
                            inventory[fish_name] = (
                                int(inventory.get(fish_name, 0)) + amount
                            )
                            await user_config.inventory.set(inventory)
                            completion_prompts = (
                                await self._newly_completed_collections(
                                    interaction.user,
                                    inventory,
                                )
                            )

                        # Every cast, including Nothing, uses one cast of bait.
                        remaining_casts -= 1
                        if remaining_casts <= 0 and cooldown > 0:
                            cooldown_until = time.time() + cooldown
                            remaining_casts = 0
                            bait_status = (
                                f"Bait exhausted. Reload in **{cooldown}** seconds."
                            )
                        elif remaining_casts <= 0:
                            remaining_casts = casts_per_bait
                            cooldown_until = 0
                            bait_status = (
                                f"Bait refilled: **{casts_per_bait}** casts ready."
                            )
                        else:
                            cooldown_until = 0
                            bait_status = (
                                f"**{remaining_casts}** cast"
                                f"{'s' if remaining_casts != 1 else ''} "
                                "remaining before reload."
                            )

                        await user_config.bait_state.set({
                            "casts_remaining": remaining_casts,
                            "cooldown_until": cooldown_until,
                        })

                except (KeyError, TypeError, ValueError, OverflowError):
                    config_error = True
                    log.exception("Fishing configuration error at %s", location_key)

        if location_missing:
            await interaction.followup.send(
                "That fishing location is not configured.",
                ephemeral=True,
            )
            return
        if cooldown_message:
            await interaction.followup.send(cooldown_message, ephemeral=True)
            return
        if config_error:
            await interaction.followup.send(
                "Fishing or bait settings are misconfigured. Ask an admin to "
                "check the dashboard or `!lokfish settings`.",
                ephemeral=True,
            )
            return

        if no_catch:
            embed = discord.Embed(
                title="No catch this time",
                description=f"You cast at **{location['label']}**, but caught nothing.",
                color=discord.Color.dark_grey(),
            )
            embed.add_field(name="Rarity roll", value=rarity.title())
            embed.add_field(name="Bait", value=bait_status, inline=False)
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        embed = discord.Embed(
            title=f"You caught {fish_name}!",
            description=(
                f"**{amount} × {fish_name}** caught at **{location['label']}**."
            ),
            color=discord.Color.gold() if rarity == "rare" else discord.Color.blue(),
        )
        embed.add_field(name="Rarity", value=rarity.title())
        embed.add_field(name="Added to inventory", value=str(amount))
        embed.add_field(name="Bait", value=bait_status, inline=False)

        if fish_description:
            embed.add_field(
                name="About this fish",
                value=fish_description,
                inline=False,
            )

        avatar = await asyncio.to_thread(self._make_avatar, fish_name)
        if avatar:
            image_bytes, filename = avatar
            embed.set_thumbnail(url=f"attachment://{filename}")
            await interaction.followup.send(
                embed=embed,
                file=discord.File(io.BytesIO(image_bytes), filename=filename),
                ephemeral=True,
            )
        else:
            await interaction.followup.send(embed=embed, ephemeral=True)

        for prompt in completion_prompts:
            await interaction.followup.send(
                prompt,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

        if (
            fish_chance is not None
            and fish_chance <= ULTRARARE_THRESHOLD_PERCENT
            and interaction.guild is not None
            and interaction.channel is not None
        ):
            try:
                await interaction.channel.send(
                    f"✨ {interaction.user.mention} caught an ultra-rare "
                    f"**{fish_name}** at **{location['label']}**! "
                    f"(about **{fish_chance:.2f}%** chance per cast)",
                    delete_after=3600,
                    allowed_mentions=discord.AllowedMentions(
                        users=True,
                        roles=False,
                        everyone=False,
                    ),
                )
            except discord.HTTPException:
                log.exception("Could not send ultra-rare announcement.")

    @staticmethod
    def _make_avatar(fish_name):
        slug = fish_slug(fish_name)
        path = next(
            (
                AVATAR_DIR / f"{slug}{extension}"
                for extension in IMAGE_EXTENSIONS
                if (AVATAR_DIR / f"{slug}{extension}").is_file()
            ),
            None,
        )
        if path is None:
            return None

        try:
            from PIL import Image

            with Image.open(path) as source:
                image = source.convert("RGBA")
                resampling = getattr(Image, "Resampling", Image)
                image = image.resize((300, 300), resampling.LANCZOS)
                buffer = io.BytesIO()
                image.save(buffer, format="PNG")
                return buffer.getvalue(), "fish-avatar.png"
        except Exception:
            log.exception("Could not process avatar %s", path)
            return None

    async def _send_inventory(self, ctx, member=None):
        member = member or ctx.author
        inventory = await self.config.user(member).inventory()

        embed = discord.Embed(
            title=f"{member.display_name}'s Fish Inventory",
            color=discord.Color.blue(),
        )
        if not inventory:
            embed.description = "No fish caught yet. Use `!lokfish` to start fishing."
        else:
            entries = sorted(
                inventory.items(),
                key=lambda item: (-int(item[1]), item[0].casefold()),
            )
            lines = [f"**{name}** — {count}" for name, count in entries]
            embed.description = "\n".join(lines)[:3900]

        await ctx.send(embed=embed)

    @commands.command(name="inventory", aliases=["fishinventory"])
    async def inventory_command(self, ctx, member: discord.Member = None):
        """Show a player's fish inventory."""
        await self._send_inventory(ctx, member)

    @lokfish.command(name="profile", aliases=["inventory"])
    async def profile(self, ctx, member: discord.Member = None):
        """Show a player's fish inventory."""
        await self._send_inventory(ctx, member)

    @commands.command(name="collection", aliases=["collections"])
    async def collection_command(self, ctx):
        """Show progress for all global fish collections."""
        collections = await self.config.collections()
        inventory = await self.config.user(ctx.author).inventory()
        owned = {
            str(name).casefold()
            for name, amount in inventory.items()
            if int(amount) > 0
        }

        lines = []
        for key, collection in collections.items():
            name = str(collection.get("name", key))
            if not collection.get("enabled", True):
                lines.append(f"**{name}** — disabled")
                continue

            required = collection.get("fish", [])
            missing = [
                fish for fish in required
                if str(fish).casefold() not in owned
            ]

            if missing:
                lines.append(
                    f"**{name}** — {len(required) - len(missing)}/{len(required)}\n"
                    f"Missing: {', '.join(missing)}"
                )
            else:
                lines.append(f"✅ **{name}** — complete")

        description = "\n\n".join(lines) or "No collections configured."
        embed = discord.Embed(
            title=f"{ctx.author.display_name}'s Collections",
            description=description[:3900],
            color=discord.Color.green(),
        )
        await ctx.send(embed=embed)

    @lokfish.command(name="settings")
    @checks.admin_or_permissions(manage_guild=True)
    async def settings(self, ctx):
        locations = await self.config.locations()
        casts = await self.config.casts_per_bait()
        cooldown = await self.config.bait_cooldown_seconds()

        embed = discord.Embed(
            title="Fishing Settings",
            description=(
                f"Casts per bait: **{casts}**\n"
                f"Bait reload cooldown: **{cooldown} seconds**\n"
                "Each cast, including Nothing, uses bait. "
                "Edit bait settings and table percentages in the dashboard."
            ),
            color=discord.Color.blue(),
        )

        for location in locations.values():
            common = ", ".join(
                f"{entry['name']} ({entry['weight']})"
                for entry in location.get("common", [])[:10]
            ) or "—"
            rare = ", ".join(
                f"{entry['name']} ({entry['weight']})"
                for entry in location.get("rare", [])[:10]
            ) or "—"
            embed.add_field(
                name=f"{location['label']} — rare {location['rare_chance']}%",
                value=f"**Common:** {common}\n**Rare:** {rare}",
                inline=False,
            )

        await ctx.send(embed=embed)

    @lokfish.command(name="setrare")
    @checks.admin_or_permissions(manage_guild=True)
    async def setrare(self, ctx, location_name: str, percent: float):
        key = normalize_location(location_name)
        if not math.isfinite(percent) or not 0 <= percent <= 100:
            await ctx.send("Rare chance must be between 0 and 100.")
            return

        async with self._state_lock:
            locations = await self.config.locations()
            if key not in locations:
                await ctx.send("Unknown location.")
                return
            locations[key]["rare_chance"] = float(percent)
            await self.config.locations.set(locations)

        await ctx.send(f"Rare chance updated to **{percent:g}%**.")

    @lokfish.command(name="addfish")
    @checks.admin_or_permissions(manage_guild=True)
    async def addfish(self, ctx, location_name: str, rarity: str, weight: float, *, fish_name: str):
        key = normalize_location(location_name)
        rarity = rarity.lower()
        fish_name = fish_name.strip()

        if rarity not in RARITIES:
            await ctx.send("Rarity must be common or rare.")
            return
        if not math.isfinite(weight) or weight <= 0:
            await ctx.send("Weight must be positive.")
            return
        if not fish_name or len(fish_name) > 80 or fish_name.casefold() == "nothing":
            await ctx.send("Enter a valid fish name; use addnothing for a miss.")
            return

        async with self._state_lock:
            locations = await self.config.locations()
            if key not in locations:
                await ctx.send("Unknown location.")
                return
            table = locations[key][rarity]
            if any(entry["name"].casefold() == fish_name.casefold() for entry in table):
                await ctx.send("That fish is already in the table.")
                return
            table.append({"name": fish_name, "weight": float(weight)})
            await self.config.locations.set(locations)
            await self.add_catalog_names([fish_name])

        await ctx.send(f"Added **{fish_name}** to the {rarity} table.")

    @lokfish.command(name="addnothing")
    @checks.admin_or_permissions(manage_guild=True)
    async def addnothing(self, ctx, location_name: str, weight: float):
        key = normalize_location(location_name)
        if not math.isfinite(weight) or weight <= 0:
            await ctx.send("Weight must be positive.")
            return

        async with self._state_lock:
            locations = await self.config.locations()
            if key not in locations:
                await ctx.send("Unknown location.")
                return
            table = locations[key]["common"]
            if any(is_nothing(entry) for entry in table):
                await ctx.send("Nothing is already in the common table.")
                return
            table.append({
                "name": "Nothing",
                "weight": float(weight),
                "type": "nothing",
            })
            await self.config.locations.set(locations)

        await ctx.send("Added Nothing to the common table.")

    @lokfish.command(name="removefish")
    @checks.admin_or_permissions(manage_guild=True)
    async def removefish(self, ctx, location_name: str, rarity: str, *, fish_name: str):
        key = normalize_location(location_name)
        rarity = rarity.lower()
        if rarity not in RARITIES:
            await ctx.send("Rarity must be common or rare.")
            return

        async with self._state_lock:
            locations = await self.config.locations()
            if key not in locations:
                await ctx.send("Unknown location.")
                return
            table = locations[key][rarity]
            entry = next(
                (item for item in table
                 if item["name"].casefold() == fish_name.strip().casefold()),
                None,
            )
            if entry is None:
                await ctx.send("That entry is not in the table.")
                return
            if len(table) <= 1:
                await ctx.send("Each table must retain at least one entry.")
                return

            table.remove(entry)
            await self.config.locations.set(locations)

        await ctx.send(f"Removed **{entry['name']}** from the {rarity} table.")