import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite
from datetime import datetime

from cogs.config import get_id

# ──────────────────────────────────────────────
#  CONFIG — настраивается командами /config:
#   hr_role_id    — роль HR (может давать/снимать очки)
#   admin_role_id — роль Admin
#   vip_role_id   — роль, выдаваемая за покупку товара "vip"
# ──────────────────────────────────────────────

# Товары магазина — редактируй под свой сервер
# Формат: "название": {"price": цена, "role_key": ключ конфига роли или None, "description": "описание"}
SHOP_ITEMS: dict[str, dict] = {
    "vip": {
        "price": 500,
        "role_key": "vip_role_id",   # роль настраивается через /config
        "description": "Роль VIP на сервере",
        "emoji": "⭐",
    },
    "custom_nick": {
        "price": 300,
        "role_key": None,
        "description": "Кастомный ник (обратись к HR)",
        "emoji": "✏️",
    },
    "car_slot": {
        "price": 800,
        "role_key": None,
        "description": "Дополнительный слот машины",
        "emoji": "🚗",
    },
}

DB = "bot.db"


async def _is_hr(interaction: discord.Interaction) -> bool:
    if interaction.user.guild_permissions.administrator:
        return True
    role_ids = {r.id for r in interaction.user.roles}
    hr_role_id = await get_id(interaction.guild_id, "hr_role_id")
    admin_role_id = await get_id(interaction.guild_id, "admin_role_id")
    return (bool(hr_role_id) and hr_role_id in role_ids) or (bool(admin_role_id) and admin_role_id in role_ids)


async def get_points(user_id: int) -> int:
    async with aiosqlite.connect(DB) as db:
        async with db.execute(
            "SELECT points FROM shop_points WHERE user_id=?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
    return row[0] if row else 0


async def add_points(user_id: int, amount: int) -> int:
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            """INSERT INTO shop_points (user_id, points) VALUES (?,?)
               ON CONFLICT(user_id) DO UPDATE SET points = points + excluded.points, last_updated=CURRENT_TIMESTAMP""",
            (user_id, amount),
        )
        await db.commit()
        async with db.execute("SELECT points FROM shop_points WHERE user_id=?", (user_id,)) as cur:
            row = await cur.fetchone()
    return row[0] if row else 0


async def spend_points(user_id: int, amount: int) -> bool:
    current = await get_points(user_id)
    if current < amount:
        return False
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "UPDATE shop_points SET points = points - ?, last_updated=CURRENT_TIMESTAMP WHERE user_id=?",
            (amount, user_id),
        )
        await db.commit()
    return True


class ShopSelectView(discord.ui.View):
    def __init__(self, user: discord.Member, guild: discord.Guild):
        super().__init__(timeout=60)
        self.user = user
        self.guild = guild

        options = [
            discord.SelectOption(
                label=f"{info['emoji']} {item_key} — {info['price']} очков",
                value=item_key,
                description=info["description"],
            )
            for item_key, info in SHOP_ITEMS.items()
        ]
        select = discord.ui.Select(
            placeholder="Выбери товар...",
            options=options,
            custom_id="shop_item_select",
        )
        select.callback = self.select_callback
        self.add_item(select)

    async def select_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("❌ Это не твой магазин.", ephemeral=True)
            return

        item_key = interaction.data["values"][0]
        item = SHOP_ITEMS.get(item_key)
        if not item:
            await interaction.response.send_message("⚠️ Товар не найден.", ephemeral=True)
            return

        current = await get_points(interaction.user.id)
        if current < item["price"]:
            await interaction.response.send_message(
                f"❌ Недостаточно очков! У тебя **{current}** оч., нужно **{item['price']}** оч.",
                ephemeral=True,
            )
            return

        # Покупаем
        await spend_points(interaction.user.id, item["price"])

        # Логируем покупку
        async with aiosqlite.connect(DB) as db:
            await db.execute(
                "INSERT INTO shop_history (user_id, item_key, price, bought_at) VALUES (?,?,?,CURRENT_TIMESTAMP)",
                (interaction.user.id, item_key, item["price"]),
            )
            await db.commit()

        # Выдать роль если есть
        role_key = item.get("role_key")
        if role_key:
            role_id = await get_id(interaction.guild_id, role_key)
            if role_id:
                role = self.guild.get_role(role_id)
                if role:
                    try:
                        await interaction.user.add_roles(role, reason=f"Покупка в магазине: {item_key}")
                    except discord.Forbidden:
                        pass

        new_balance = await get_points(interaction.user.id)
        embed = discord.Embed(
            title="🛒 Покупка совершена!",
            description=(
                f"**Товар:** {item['emoji']} {item_key}\n"
                f"**Цена:** {item['price']} очков\n"
                f"**Остаток:** {new_balance} очков"
            ),
            color=discord.Color.green(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class ShopCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="shop", description="Открыть магазин семьи")
    async def shop_cmd(self, interaction: discord.Interaction):
        current = await get_points(interaction.user.id)

        embed = discord.Embed(
            title="🏪 Магазин",
            description=f"💰 У тебя **{current}** очков\n\nВыбери товар ниже:",
            color=discord.Color.gold(),
        )
        for item_key, info in SHOP_ITEMS.items():
            embed.add_field(
                name=f"{info['emoji']} {item_key}",
                value=f"💰 {info['price']} оч. — {info['description']}",
                inline=False,
            )
        embed.set_footer(text="ETERNAL HELPER • Магазин семьи")

        await interaction.response.send_message(
            embed=embed,
            view=ShopSelectView(interaction.user, interaction.guild),
            ephemeral=True,
        )

    @app_commands.command(name="points", description="Посмотреть баланс очков")
    @app_commands.describe(member="Участник (по умолчанию — ты)")
    async def points_cmd(self, interaction: discord.Interaction, member: discord.Member | None = None):
        target = member or interaction.user
        pts = await get_points(target.id)

        embed = discord.Embed(
            title=f"💰 Очки — {target.display_name}",
            description=f"**{pts}** очков",
            color=discord.Color.gold(),
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="points_give", description="[HR] Выдать очки участнику")
    @app_commands.describe(member="Участник", amount="Количество очков", reason="Причина")
    async def points_give(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        amount: int,
        reason: str = "Без причины",
    ):
        if not await _is_hr(interaction):
            await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
            return

        if amount <= 0:
            await interaction.response.send_message("⚠️ Укажи положительное количество.", ephemeral=True)
            return

        new_balance = await add_points(member.id, amount)

        embed = discord.Embed(
            title="💰 Очки выданы",
            description=(
                f"**Кому:** {member.mention}\n"
                f"**Сколько:** +{amount} очков\n"
                f"**Причина:** {reason}\n"
                f"**Баланс теперь:** {new_balance} очков"
            ),
            color=discord.Color.green(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

        try:
            dm_embed = discord.Embed(
                title="💰 Тебе начислены очки!",
                description=f"+**{amount}** очков\nПричина: *{reason}*\nТвой баланс: **{new_balance}** оч.",
                color=discord.Color.green(),
            )
            await member.send(embed=dm_embed)
        except discord.Forbidden:
            pass

    @app_commands.command(name="points_take", description="[HR] Снять очки с участника")
    @app_commands.describe(member="Участник", amount="Количество очков", reason="Причина")
    async def points_take(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        amount: int,
        reason: str = "Без причины",
    ):
        if not await _is_hr(interaction):
            await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
            return

        if amount <= 0:
            await interaction.response.send_message("⚠️ Укажи положительное количество.", ephemeral=True)
            return

        success = await spend_points(member.id, amount)
        if not success:
            current = await get_points(member.id)
            await interaction.response.send_message(
                f"⚠️ У {member.mention} только **{current}** очков, не хватает на списание {amount}.",
                ephemeral=True,
            )
            return

        new_balance = await get_points(member.id)
        await interaction.response.send_message(
            f"✅ Списано **{amount}** очков с {member.mention}. Остаток: **{new_balance}** оч.\nПричина: *{reason}*",
            ephemeral=True,
        )

    @app_commands.command(name="points_top", description="Топ по очкам")
    async def points_top(self, interaction: discord.Interaction):
        async with aiosqlite.connect(DB) as db:
            async with db.execute(
                "SELECT user_id, points FROM shop_points ORDER BY points DESC LIMIT 10"
            ) as cur:
                rows = await cur.fetchall()

        if not rows:
            await interaction.response.send_message("📭 Нет данных.", ephemeral=True)
            return

        medals = ["🥇", "🥈", "🥉"]
        lines = [
            f"{medals[i] if i < 3 else f'`{i+1}.`'} <@{uid}> — **{pts}** оч."
            for i, (uid, pts) in enumerate(rows)
        ]
        embed = discord.Embed(
            title="🏆 Топ по очкам",
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="shop_history", description="История твоих покупок")
    async def shop_history(self, interaction: discord.Interaction):
        async with aiosqlite.connect(DB) as db:
            async with db.execute(
                "SELECT item_key, price, bought_at FROM shop_history WHERE user_id=? ORDER BY bought_at DESC LIMIT 10",
                (interaction.user.id,),
            ) as cur:
                rows = await cur.fetchall()

        if not rows:
            await interaction.response.send_message("📭 У тебя нет покупок.", ephemeral=True)
            return

        lines = [
            f"• **{item}** — {price} оч. (<t:{int(datetime.fromisoformat(ts).timestamp())}:R>)"
            for item, price, ts in rows
        ]
        embed = discord.Embed(
            title="🛒 История покупок",
            description="\n".join(lines),
            color=discord.Color.blue(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(ShopCog(bot))