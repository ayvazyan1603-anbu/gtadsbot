import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite
from datetime import datetime
import asyncio

from cogs.config import get_id

# ──────────────────────────────────────────────
#  CONFIG — настраивается командой /config:
#   hr_role_id    — роль HR (может делать рассылки)
#   admin_role_id — роль Admin
# ──────────────────────────────────────────────

DB = "bot.db"


async def _is_hr(interaction: discord.Interaction) -> bool:
    if interaction.user.guild_permissions.administrator:
        return True
    role_ids = {r.id for r in interaction.user.roles}
    hr_role_id = await get_id(interaction.guild_id, "hr_role_id")
    admin_role_id = await get_id(interaction.guild_id, "admin_role_id")
    return (bool(hr_role_id) and hr_role_id in role_ids) or (bool(admin_role_id) and admin_role_id in role_ids)


# ════════════════════════════════════════════════
#  MODAL — составление сообщения для рассылки
# ════════════════════════════════════════════════

class BroadcastModal(discord.ui.Modal, title="Рассылка по роли"):
    message_text = discord.ui.TextInput(
        label="Текст сообщения",
        style=discord.TextStyle.long,
        placeholder="Введи сообщение, которое получат все с этой ролью...",
        max_length=1800,
    )

    def __init__(self, role: discord.Role):
        super().__init__()
        self.role = role

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        members = [m for m in self.role.members if not m.bot]
        if not members:
            await interaction.followup.send(
                f"⚠️ У роли **{self.role.name}** нет участников.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title=f"📨 Сообщение от {interaction.guild.name}",
            description=self.message_text.value,
            color=discord.Color.blue(),
            timestamp=datetime.utcnow(),
        )
        embed.set_footer(text=f"Отправил: {interaction.user.display_name} | Роль: {self.role.name}")

        sent, failed = 0, 0
        for member in members:
            try:
                await member.send(embed=embed)
                sent += 1
                await asyncio.sleep(0.5)  # Защита от рейт-лимита Discord
            except (discord.Forbidden, discord.HTTPException):
                failed += 1

        result = (
            f"✅ Рассылка завершена!\n"
            f"📨 Отправлено: **{sent}**\n"
            f"❌ Не доставлено: **{failed}** (закрытые ЛС)\n"
            f"👥 Роль: **{self.role.name}**"
        )
        await interaction.followup.send(result, ephemeral=True)


class RoleSelectForBroadcast(discord.ui.View):
    """Промежуточный шаг — выбор роли перед показом модала."""

    def __init__(self, guild: discord.Guild, author: discord.Member):
        super().__init__(timeout=60)
        self.guild = guild
        self.author = author

    @discord.ui.select(
        cls=discord.ui.RoleSelect,
        placeholder="Начни вводить название роли...",
        min_values=1,
        max_values=1,
    )
    async def role_selected(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        if interaction.user.id != self.author.id:
            await interaction.response.send_message("❌ Это не твой выбор.", ephemeral=True)
            return

        role = select.values[0]
        await interaction.response.send_modal(BroadcastModal(role))


# ════════════════════════════════════════════════
#  МП (МЕРОПРИЯТИЕ) — сбор реакций
# ════════════════════════════════════════════════

class EventModal(discord.ui.Modal, title="Создать мероприятие (МП)"):
    event_title = discord.ui.TextInput(
        label="Название МП",
        placeholder="Например: Совместный выезд на арену",
        max_length=100,
    )
    event_desc = discord.ui.TextInput(
        label="Описание",
        style=discord.TextStyle.long,
        placeholder="Опиши мероприятие, время, место...",
        max_length=1000,
    )
    event_time = discord.ui.TextInput(
        label="Время проведения",
        placeholder="Например: 20:00 по МСК",
        max_length=100,
    )

    def __init__(self, ping_role: discord.Role | None = None):
        super().__init__()
        self.ping_role = ping_role

    async def on_submit(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title=f"📅 МП: {self.event_title.value}",
            description=self.event_desc.value,
            color=discord.Color.purple(),
            timestamp=datetime.utcnow(),
        )
        embed.add_field(name="⏰ Время", value=self.event_time.value, inline=True)
        embed.add_field(name="👤 Организатор", value=interaction.user.mention, inline=True)
        embed.set_footer(text="✅ — Буду  |  ❌ — Не буду  |  ❓ — Возможно")

        ping = self.ping_role.mention if self.ping_role else ""
        msg = await interaction.channel.send(content=ping, embed=embed)

        # Добавляем реакции
        for emoji in ["✅", "❌", "❓"]:
            await msg.add_reaction(emoji)

        # Сохраняем в БД для отслеживания
        async with aiosqlite.connect(DB) as db:
            await db.execute(
                """INSERT INTO events (msg_id, channel_id, title, organizer_id, created_at)
                   VALUES (?,?,?,?,CURRENT_TIMESTAMP)""",
                (msg.id, interaction.channel_id, self.event_title.value, interaction.user.id),
            )
            await db.commit()

        await interaction.response.send_message("✅ МП создано!", ephemeral=True)


class BroadcastCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── Рассылка по роли ───────────────────────

    @app_commands.command(name="dm_role", description="[HR] Отправить сообщение в ЛС всем с ролью")
    async def dm_role(self, interaction: discord.Interaction):
        if not await _is_hr(interaction):
            await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
            return

        await interaction.response.send_message(
            "📨 Выбери роль для рассылки:",
            view=RoleSelectForBroadcast(interaction.guild, interaction.user),
            ephemeral=True,
        )

    # ── МП / Мероприятие ───────────────────────

    @app_commands.command(name="event", description="Создать мероприятие (МП) со сбором реакций")
    @app_commands.describe(ping_role="Роль, которую пинговать (необязательно)")
    async def event_cmd(
        self,
        interaction: discord.Interaction,
        ping_role: discord.Role | None = None,
    ):
        if not await _is_hr(interaction):
            await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
            return

        await interaction.response.send_modal(EventModal(ping_role))

    @app_commands.command(name="event_stats", description="Статистика реакций на МП")
    @app_commands.describe(message_id="ID сообщения с МП")
    async def event_stats(self, interaction: discord.Interaction, message_id: str):
        if not await _is_hr(interaction):
            await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
            return

        try:
            msg_id = int(message_id)
        except ValueError:
            await interaction.response.send_message("⚠️ Неверный ID сообщения.", ephemeral=True)
            return

        # Ищем сообщение в текущем канале
        try:
            msg = await interaction.channel.fetch_message(msg_id)
        except discord.NotFound:
            await interaction.response.send_message(
                "⚠️ Сообщение не найдено в этом канале.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        embed = discord.Embed(
            title="📊 Статистика МП",
            color=discord.Color.purple(),
        )

        emoji_map = {"✅": "Буду", "❌": "Не буду", "❓": "Возможно"}
        for reaction in msg.reactions:
            if str(reaction.emoji) in emoji_map:
                users = [u async for u in reaction.users() if not u.bot]
                label = emoji_map.get(str(reaction.emoji), str(reaction.emoji))
                user_list = ", ".join(u.display_name for u in users[:20]) if users else "Никого"
                embed.add_field(
                    name=f"{reaction.emoji} {label} ({len(users)})",
                    value=user_list[:1024],
                    inline=False,
                )

        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(BroadcastCog(bot))