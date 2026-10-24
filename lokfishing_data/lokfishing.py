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
MAX_FISH_DESCRIPTION_LENGTH = 1000
MAX_MENU_LOCATIONS = 25

RARITIES = {"common", "rare"}
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")


def fish_slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "fish"


def normalize_location(value: str) -> str:
    return re.sub(r"[\s-]+", "_", value.strip().lower())


def is_nothing(entry: dict) -> bool:
    return (
        entry.get("type") == "nothing"
        or str(entry.get("name", "")).strip().casefold() == "nothing"
    )


def load_default_locations() -> dict:
    """Load and validate default drop tables from the adjacent JSON file."""
    table_path = Path(__file__).with_name("loot_tables.json")

    try:
        with table_path.open("r", encoding="utf-8") as table_file:
            locations = json.load(table_file)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Could not load fishing tables from {table_path}: {exc}"
        ) from exc

    if not isinstance(locations, dict) or not locations:
        raise RuntimeError(
            f"{table_path} must contain a non-empty JSON object."
        )

    for key, location in locations.items():
        if not isinstance(key, str) or not re.fullmatch(r"[a-z0-9_]+", key):
            raise RuntimeError(f"Invalid location key: {key!r}")
        if not isinstance(location, dict):
            raise RuntimeError(f"Location {key!r} must be an object.")
        if not isinstance(location.get("label"), str) or not location["label"].strip():
            raise RuntimeError(f"Location {key!r} needs a non-empty label.")

        try:
            rare_chance = float(location["rare_chance"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Location {key!r} needs a valid rare_chance."
            ) from exc

        if not math.isfinite(rare_chance) or not 0 <= rare_chance <= 100:
            raise RuntimeError(
                f"Location {key!r} rare_chance must be between 0 and 100."
            )

        for rarity in RARITIES:
            table = location.get(rarity)
            if not isinstance(table, list) or not table:
                raise RuntimeError(
                    f"Location {key!r} needs a non-empty {rarity} table."
                )

            for entry in table:
                if not isinstance(entry, dict):
                    raise RuntimeError(
                        f"Location {key!r} has an invalid {rarity} entry."
                    )
                if not isinstance(entry.get("name"), str) or not entry["name"].strip():
                    raise RuntimeError(
                        f"Location {key!r} has an entry without a name."
                    )

                try:
                    weight = float(entry.get("weight", 0))
                except (TypeError, ValueError) as exc:
                    raise RuntimeError(
                        f"Location {key!r} has an invalid weight."
                    ) from exc

                if not math.isfinite(weight) or weight <= 0:
                    raise RuntimeError(
                        f"Location {key!r} weights must be positive."
                    )

    return locations


DEFAULT_LOCATIONS = load_default_locations()


class LocationButton(discord.ui.Button):
    def __init__(self, cog, location_key: str, label: str):
        super().__init__(
            label=label,
            style=discord.ButtonStyle.primary,
            custom_id=f"lokfishing:{location_key}",
        )
        self.cog = cog
        self.location_key = location_key

    async def callback(self, interaction: discord.Interaction):
        await self.cog.handle_catch(interaction, self.location_key)


class FishingMenu(discord.ui.View):
    def __init__(self, cog, locations: dict):
        super().__init__(timeout=900)
        self.message = None

        for key, location in list(locations.items())[:MAX_MENU_LOCATIONS]:
            self.add_item(
                LocationButton(
                    cog=cog,
                    location_key=key,
                    label=location["label"],
                )
            )

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
    """Button-driven fishing game with bait, collections, and dashboard."""

    default_locations = DEFAULT_LOCATIONS

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(
            self,
            identifier=0x4C4F4B46495348,
            force_registration=True,
        )
        self.config.register_global(
            locations=DEFAULT_LOCATIONS,
            cooldown_seconds=30,
            casts_per_bait=6,
            bait_cooldown_seconds=30,
            fish_descriptions={},
            collections={},
        )
        self.config.register_user(
            inventory={},
            completed_collections={},
            bait_state={},
        )

        self._last_catch = {}
        self._state_lock = asyncio.Lock()
        self._dashboard = None

    async def _ensure_default_collections(self):
        """Create an initial common-fish collection for locations lacking one."""
        async with self._state_lock:
            locations = await self.config.locations()
            collections = await self.config.collections()
            changed = False

            for key, location in locations.items():
                if key in collections:
                    continue

                fish = []
                seen = set()

                for entry in location.get("common", []):
                    if not isinstance(entry, dict) or is_nothing(entry):
                        continue

                    name = str(entry.get("name", "")).strip()
                    if name and name.casefold() not in seen:
                        fish.append(name)
                        seen.add(name.casefold())

                collections[key] = {
                    "enabled": bool(fish),
                    "fish": fish,
                    "prompt": DEFAULT_COLLECTION_PROMPT,
                }
                changed = True

            if changed:
                await self.config.collections.set(collections)

    async def cog_load(self):
        await self._ensure_default_collections()

        password = os.getenv("LOKFISHING_DASHBOARD_PASSWORD")
        if not password:
            log.info(
                "LokFishing dashboard disabled: "
                "LOKFISHING_DASHBOARD_PASSWORD is not set."
            )
            return

        if len(password) < 24:
            log.error(
                "LokFishing dashboard disabled: password must be at least "
                "24 characters long."
            )
            return

        try:
            port = int(os.getenv("LOKFISHING_DASHBOARD_PORT", "8788"))
            if not 1 <= port <= 65535:
                raise ValueError("Dashboard port must be between 1 and 65535.")

            self._dashboard = FishingDashboard(self, password, port)
            await self._dashboard.start()
            log.info("LokFishing dashboard started on port %s.", port)
        except Exception:
            self._dashboard = None
            log.exception("Could not start the LokFishing dashboard.")

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
            "**Legend of Korra Fishing**\n"
            "Choose a location to cast your line:",
            view=view,
        )
        view.message = message

        if len(locations) > MAX_MENU_LOCATIONS:
            await ctx.send(
                f"Only the first {MAX_MENU_LOCATIONS} locations fit in the menu."
            )

    @staticmethod
    def _validated_table(location: dict, rarity: str):
        table = location.get(rarity)
        if not isinstance(table, list) or not table:
            raise ValueError(f"The {rarity} table is empty or invalid.")

        weights = []
        for entry in table:
            if not isinstance(entry, dict):
                raise ValueError(f"The {rarity} table has an invalid entry.")

            name = entry.get("name")
            if not isinstance(name, str) or not name.strip():
                raise ValueError(f"The {rarity} table has an entry without a name.")

            weight = float(entry.get("weight", 0))
            if not math.isfinite(weight) or weight <= 0:
                raise ValueError(f"The {rarity} table has an invalid weight.")

            weights.append(weight)

        return table, weights

    def _fish_chance_percent(
        self,
        location: dict,
        rare_chance: float,
        fish_name: str,
    ) -> float:
        total = 0.0
        branch_chances = {
            "common": 100.0 - rare_chance,
            "rare": rare_chance,
        }

        for rarity, branch_chance in branch_chances.items():
            if branch_chance <= 0:
                continue

            table, weights = self._validated_table(location, rarity)
            table_total = sum(weights)

            for entry, weight in zip(table, weights):
                if (
                    not is_nothing(entry)
                    and entry["name"].strip().casefold()
                    == fish_name.strip().casefold()
                ):
                    total += branch_chance * weight / table_total

        return min(total, 100.0)

    async def _record_new_collection_completions(
        self,
        user,
        inventory: dict,
        locations: dict,
    ) -> list:
        """Save newly completed collection versions and return their prompts."""
        collections = await self.config.collections()
        user_config = self.config.user(user)
        completed = await user_config.completed_collections()

        if not isinstance(completed, dict):
            completed = {}

        owned = {
            str(name).casefold()
            for name, amount in inventory.items()
            if int(amount) > 0
        }

        prompts = []
        changed = False

        for key, collection in collections.items():
            if not isinstance(collection, dict) or not collection.get("enabled", True):
                continue

            required = collection.get("fish", [])
            if not isinstance(required, list) or not required:
                continue

            normalized = sorted(
                {
                    str(name).strip().casefold()
                    for name in required
                    if str(name).strip()
                }
            )
            if not normalized or not all(name in owned for name in normalized):
                continue

            signature_data = json.dumps(
                normalized,
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("utf-8")
            signature = hashlib.sha256(signature_data).hexdigest()

            if completed.get(key) == signature:
                continue

            completed[key] = signature
            changed = True

            location_label = locations.get(key, {}).get("label", key)
            collection_label = f"{location_label} Common Fish"
            prompt = str(collection.get("prompt", "")).strip()
            if not prompt:
                prompt = DEFAULT_COLLECTION_PROMPT

            prompt = (
                prompt.replace("{user}", user.display_name)
                .replace("{collection}", collection_label)
                .replace("{location}", location_label)
            )
            prompts.append(prompt[:1900])

        if changed:
            await user_config.completed_collections.set(completed)

        return prompts

    async def handle_catch(self, interaction: discord.Interaction, location_key: str):
        if interaction.user.bot:
            await interaction.response.send_message(
                "Bots can't fish.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        user_id = interaction.user.id
        current_time = time.time()

        cooldown_message = None
        config_error = False
        missing_location = False
        no_catch = False
        rarity = None
        fish_name = None
        fish_description = ""
        amount = 0
        fish_chance_percent = None
        location = None
        bait_status = ""
        completion_prompts = []

        async with self._state_lock:
            locations = await self.config.locations()
            location = locations.get(location_key)

            if location is None:
                missing_location = True
            else:
                try:
                    casts_per_bait = int(await self.config.casts_per_bait())
                    cooldown_seconds = int(
                        await self.config.bait_cooldown_seconds()
                    )

                    if not 1 <= casts_per_bait <= 100:
                        raise ValueError("Casts per bait is outside valid bounds.")
                    if not 0 <= cooldown_seconds <= 86400:
                        raise ValueError("Bait cooldown is outside valid bounds.")

                    user_config = self.config.user(interaction.user)
                    bait_state = await user_config.bait_state()
                    if not isinstance(bait_state, dict):
                        bait_state = {}

                    casts_remaining = int(
                        bait_state.get("casts_remaining", casts_per_bait)
                    )
                    cooldown_until = float(
                        bait_state.get("cooldown_until", 0)
                    )

                    if cooldown_until > current_time:
                        wait = math.ceil(cooldown_until - current_time)
                        cooldown_message = (
                            f"Your bait is reloading. Try again in "
                            f"**{wait}** second{'s' if wait != 1 else ''}."
                        )
                    else:
                        # Refill after an expired cooldown. Also clamp old
                        # saved counts if an admin lowers casts-per-bait.
                        if cooldown_until > 0 or casts_remaining <= 0:
                            casts_remaining = casts_per_bait
                            cooldown_until = 0
                        casts_remaining = min(casts_remaining, casts_per_bait)

                        rare_chance = float(location["rare_chance"])
                        if (
                            not math.isfinite(rare_chance)
                            or not 0 <= rare_chance <= 100
                        ):
                            raise ValueError(
                                "Rare chance must be between 0 and 100."
                            )

                        rarity = random.choices(
                            ["rare", "common"],
                            weights=[rare_chance, 100.0 - rare_chance],
                            k=1,
                        )[0]

                        table, weights = self._validated_table(location, rarity)
                        outcome = random.choices(
                            table,
                            weights=weights,
                            k=1,
                        )[0]
                        no_catch = is_nothing(outcome)

                        if no_catch:
                            self._last_catch[user_id] = time.monotonic()
                        else:
                            fish_name = outcome["name"]
                            fish_chance_percent = self._fish_chance_percent(
                                location,
                                rare_chance,
                                fish_name,
                            )

                            # Rare catches yield one fish; common catches yield 1–3.
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
                                await self._record_new_collection_completions(
                                    interaction.user,
                                    inventory,
                                    locations,
                                )
                            )

                            self._last_catch[user_id] = time.monotonic()

                        # Every valid cast consumes one bait, including a miss.
                        casts_remaining -= 1
                        cooldown_until = 0

                        if casts_remaining <= 0:
                            if cooldown_seconds > 0:
                                cooldown_until = time.time() + cooldown_seconds
                                bait_status = (
                                    f"Bait exhausted. Your next cast is available "
                                    f"in **{cooldown_seconds}** seconds."
                                )
                                casts_remaining = 0
                            else:
                                casts_remaining = casts_per_bait
                                bait_status = (
                                    f"Bait refilled. **{casts_remaining}** casts available."
                                )
                        else:
                            bait_status = (
                                f"**{casts_remaining}** cast"
                                f"{'s' if casts_remaining != 1 else ''} "
                                "remaining before reloading."
                            )

                        await user_config.bait_state.set(
                            {
                                "casts_remaining": casts_remaining,
                                "cooldown_until": cooldown_until,
                            }
                        )

                except (KeyError, TypeError, ValueError, OverflowError):
                    config_error = True
                    log.exception(
                        "Invalid fishing configuration for %s",
                        location_key,
                    )

        if missing_location:
            await interaction.followup.send(
                "That fishing location is not configured.",
                ephemeral=True,
            )
            return

        if cooldown_message:
            await interaction.followup.send(
                cooldown_message,
                ephemeral=True,
            )
            return

        if config_error:
            await interaction.followup.send(
                "This location or bait setting is misconfigured. "
                "Ask an admin to check the dashboard or `!lokfish settings`.",
                ephemeral=True,
            )
            return

        if no_catch:
            embed = discord.Embed(
                title="No catch this time",
                description=(
                    f"You cast your line at **{location['label']}**, "
                    "but caught nothing."
                ),
                color=discord.Color.dark_grey(),
            )
            embed.add_field(name="Rarity roll", value=rarity.title())
            embed.add_field(name="Bait", value=bait_status, inline=False)
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        embed = discord.Embed(
            title=f"You caught {fish_name}!",
            description=(
                f"**{amount} × {fish_name}** caught at "
                f"**{location['label']}**."
            ),
            color=(
                discord.Color.gold()
                if rarity == "rare"
                else discord.Color.blue()
            ),
        )
        embed.add_field(name="Rarity", value=rarity.title())
        embed.add_field(name="Added to your inventory", value=str(amount))
        embed.add_field(name="Bait", value=bait_status, inline=False)

        if fish_description:
            embed.add_field(
                name="About this fish",
                value=fish_description,
                inline=False,
            )

        avatar_data = await asyncio.to_thread(self._make_avatar, fish_name)

        if avatar_data is not None:
            image_bytes, filename = avatar_data
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
            fish_chance_percent is not None
            and fish_chance_percent <= ULTRARARE_THRESHOLD_PERCENT
            and interaction.guild is not None
            and interaction.channel is not None
        ):
            try:
                await interaction.channel.send(
                    f"✨ {interaction.user.mention} caught an ultra-rare "
                    f"**{fish_name}** at **{location['label']}**! "
                    f"(about **{fish_chance_percent:.2f}%** chance per cast)",
                    delete_after=3600,
                    allowed_mentions=discord.AllowedMentions(
                        users=True,
                        roles=False,
                        everyone=False,
                    ),
                )
            except discord.HTTPException:
                log.exception("Could not send ultra-rare catch announcement.")

    @staticmethod
    def _make_avatar(fish_name: str):
        """Load a local avatar, resize to 300×300, and return PNG bytes."""
        slug = fish_slug(fish_name)
        image_path = next(
            (
                AVATAR_DIR / f"{slug}{extension}"
                for extension in IMAGE_EXTENSIONS
                if (AVATAR_DIR / f"{slug}{extension}").is_file()
            ),
            None,
        )

        if image_path is None:
            return None

        try:
            from PIL import Image

            with Image.open(image_path) as source:
                image = source.convert("RGBA")
                resampling = getattr(Image, "Resampling", Image)
                image = image.resize((300, 300), resampling.LANCZOS)

                buffer = io.BytesIO()
                image.save(buffer, format="PNG")
                return buffer.getvalue(), "fish-avatar.png"

        except Exception:
            log.exception("Could not process fish avatar %s", image_path)
            return None

    async def _send_inventory(self, ctx, member=None):
        member = member or ctx.author
        inventory = await self.config.user(member).inventory()

        embed = discord.Embed(
            title=f"{member.display_name}'s Fish Inventory",
            color=discord.Color.blue(),
        )

        if not inventory:
            embed.description = (
                "No fish caught yet. Use `!lokfish` to start fishing."
            )
        else:
            entries = sorted(
                inventory.items(),
                key=lambda item: (-int(item[1]), item[0].lower()),
            )
            lines = [f"**{name}** — {amount}" for name, amount in entries]
            description = "\n".join(lines)

            if len(description) > 3900:
                description = description[:3850].rsplit("\n", 1)[0]
                description += "\n…inventory truncated."

            embed.description = description

        await ctx.send(embed=embed)

    @commands.command(name="inventory", aliases=["fishinventory"])
    async def inventory_command(self, ctx, member: discord.Member = None):
        """Show your inventory, or another member's inventory."""
        await self._send_inventory(ctx, member)

    @lokfish.command(name="profile", aliases=["inventory"])
    async def profile(self, ctx, member: discord.Member = None):
        """Show your inventory, or another member's inventory."""
        await self._send_inventory(ctx, member)

    @commands.command(name="collection", aliases=["collections"])
    async def collection_command(self, ctx):
        """Show your progress toward each configured collection."""
        locations = await self.config.locations()
        collections = await self.config.collections()
        inventory = await self.config.user(ctx.author).inventory()

        owned = {
            str(name).casefold()
            for name, amount in inventory.items()
            if int(amount) > 0
        }

        lines = []
        for key, location in locations.items():
            collection = collections.get(key, {})

            if not collection.get("enabled", True):
                lines.append(f"**{location['label']}** — collection disabled")
                continue

            required = collection.get("fish", [])
            if not required:
                lines.append(f"**{location['label']}** — no fish configured")
                continue

            missing = [
                name for name in required
                if str(name).casefold() not in owned
            ]

            if missing:
                lines.append(
                    f"**{location['label']}** — "
                    f"{len(required) - len(missing)}/{len(required)} fish\n"
                    f"Missing: {', '.join(missing)}"
                )
            else:
                lines.append(
                    f"✅ **{location['label']}** — collection complete "
                    f"({len(required)}/{len(required)})"
                )

        description = "\n\n".join(lines) or "No collections are configured."
        if len(description) > 3900:
            description = description[:3850].rsplit("\n", 1)[0]
            description += "\n…collection list truncated."

        embed = discord.Embed(
            title=f"{ctx.author.display_name}'s Collections",
            description=description,
            color=discord.Color.green(),
        )
        await ctx.send(embed=embed)

    @lokfish.command(name="settings")
    @checks.admin_or_permissions(manage_guild=True)
    async def settings(self, ctx):
        """Show fishing rates, tables, and bait settings."""
        locations = await self.config.locations()
        casts = await self.config.casts_per_bait()
        cooldown = await self.config.bait_cooldown_seconds()

        embed = discord.Embed(
            title="Fishing Settings",
            description=(
                f"Casts per bait: **{casts}**\n"
                f"Bait cooldown: **{cooldown} seconds**\n"
                "Bait settings are editable in the web dashboard. "
                "Each valid cast, including Nothing, uses one bait."
            ),
            color=discord.Color.blue(),
        )

        for location in locations.values():
            common = ", ".join(
                f"{entry['name']} (weight {entry['weight']})"
                for entry in location.get("common", [])[:10]
            ) or "—"
            rare = ", ".join(
                f"{entry['name']} (weight {entry['weight']})"
                for entry in location.get("rare", [])[:10]
            ) or "—"

            embed.add_field(
                name=(
                    f"{location['label']} — "
                    f"rare chance {location['rare_chance']}%"
                ),
                value=f"**Common:** {common}\n**Rare:** {rare}",
                inline=False,
            )

        await ctx.send(embed=embed)

    @lokfish.command(name="setrare")
    @checks.admin_or_permissions(manage_guild=True)
    async def setrare(self, ctx, location_name: str, percent: float):
        """Set a location's rare chance."""
        location_key = normalize_location(location_name)

        if not math.isfinite(percent) or not 0 <= percent <= 100:
            await ctx.send("Rare chance must be between 0 and 100.")
            return

        async with self._state_lock:
            locations = await self.config.locations()
            location = locations.get(location_key)

            if location is None:
                await ctx.send(
                    "Unknown location. Use the dashboard to manage locations."
                )
                return

            location["rare_chance"] = float(percent)
            await self.config.locations.set(locations)

        await ctx.send(
            f"Rare-catch chance at **{location['label']}** "
            f"is now **{percent:g}%**."
        )

    @lokfish.command(name="setcooldown")
    @checks.admin_or_permissions(manage_guild=True)
    async def setcooldown(self, ctx, seconds: int):
        """Set the legacy catch cooldown setting."""
        if not 0 <= seconds <= 86400:
            await ctx.send("Cooldown must be between 0 and 86400 seconds.")
            return

        await self.config.cooldown_seconds.set(seconds)
        await ctx.send(
            "The legacy setting was updated. Bait reload settings are managed "
            "in the dashboard."
        )

    @lokfish.command(name="addfish")
    @checks.admin_or_permissions(manage_guild=True)
    async def addfish(
        self,
        ctx,
        location_name: str,
        rarity: str,
        weight: float,
        *,
        fish_name: str,
    ):
        """Add a fish to a location's table; weight remains an admin command."""
        location_key = normalize_location(location_name)
        rarity = rarity.lower()

        if rarity not in RARITIES:
            await ctx.send("Rarity must be `common` or `rare`.")
            return
        if not math.isfinite(weight) or weight <= 0:
            await ctx.send("Weight must be a positive number.")
            return

        fish_name = fish_name.strip()
        if not fish_name or len(fish_name) > 80:
            await ctx.send("Fish names must be between 1 and 80 characters.")
            return
        if fish_name.casefold() == "nothing":
            await ctx.send("Use `addnothing` to add a no-catch outcome.")
            return

        async with self._state_lock:
            locations = await self.config.locations()
            location = locations.get(location_key)

            if location is None:
                await ctx.send(
                    "Unknown location. Use the dashboard to manage locations."
                )
                return

            table = location[rarity]
            if any(
                entry["name"].casefold() == fish_name.casefold()
                for entry in table
            ):
                await ctx.send("That fish is already in this rarity table.")
                return
            if len(table) >= 100:
                await ctx.send("That rarity table already has 100 entries.")
                return

            table.append({"name": fish_name, "weight": float(weight)})
            await self.config.locations.set(locations)

        await ctx.send(
            f"Added **{fish_name}** to the **{rarity}** table at "
            f"**{location['label']}**."
        )

    @lokfish.command(name="addnothing")
    @checks.admin_or_permissions(manage_guild=True)
    async def addnothing(self, ctx, location_name: str, weight: float):
        """Add a no-catch outcome to a location's common table."""
        location_key = normalize_location(location_name)

        if not math.isfinite(weight) or weight <= 0:
            await ctx.send("Weight must be a positive number.")
            return

        async with self._state_lock:
            locations = await self.config.locations()
            location = locations.get(location_key)

            if location is None:
                await ctx.send(
                    "Unknown location. Use the dashboard to manage locations."
                )
                return

            table = location["common"]
            if any(is_nothing(entry) for entry in table):
                await ctx.send("That common table already has a Nothing outcome.")
                return
            if len(table) >= 100:
                await ctx.send("That common table already has 100 entries.")
                return

            table.append(
                {"name": "Nothing", "weight": float(weight), "type": "nothing"}
            )
            await self.config.locations.set(locations)

        await ctx.send(
            f"Added **Nothing** to the common table at **{location['label']}**."
        )

    @lokfish.command(name="removefish")
    @checks.admin_or_permissions(manage_guild=True)
    async def removefish(
        self,
        ctx,
        location_name: str,
        rarity: str,
        *,
        fish_name: str,
    ):
        """Remove a fish or Nothing entry from a table."""
        location_key = normalize_location(location_name)
        rarity = rarity.lower()

        if rarity not in RARITIES:
            await ctx.send("Rarity must be `common` or `rare`.")
            return

        async with self._state_lock:
            locations = await self.config.locations()
            location = locations.get(location_key)

            if location is None:
                await ctx.send(
                    "Unknown location. Use the dashboard to manage locations."
                )
                return

            table = location[rarity]
            match = next(
                (
                    entry for entry in table
                    if entry["name"].strip().casefold()
                    == fish_name.strip().casefold()
                ),
                None,
            )

            if match is None:
                await ctx.send("That entry is not in the specified table.")
                return
            if len(table) <= 1:
                await ctx.send(
                    "Each rarity table must keep at least one entry."
                )
                return

            table.remove(match)
            await self.config.locations.set(locations)

        await ctx.send(
            f"Removed **{match['name']}** from the **{rarity}** table at "
            f"**{location['label']}**."
        )