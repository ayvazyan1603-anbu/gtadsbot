import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite
from datetime import datetime, timedelta

from cogs.config import get_id

# ──────────────────────────────────────────────
#  CONFIG — настраивается командами /config:
#   vacation_role_id    — роль "В отпуске" (выдаётся автоматически)
#   vacation_log_channel — канал логов отпусков
#   hr_role_id          — роль HR
#   admin_role_id       — роль Admin
MAX_VACATION_DAYS   = 30  # Максимум дней отпуска
# ──────────────────────────────────────────────

DB = "bot.db"


async def _is_hr(interaction: discord.Interaction) -> bool:
    if interaction.user.guild_permissions.administrator:
        return True
    role_ids = {r.id for r in interaction.user.roles}
    hr_role_id = await get_id(interaction.guild_id, "hr_role_id")
    admin_role_id = await get_id(interaction.guild_id, "admin_role_id")
    return (bool(hr_role_id) and hr_role_id in role_ids) or (bool(admin_role_id) and admin_role_id in role_ids)


async def _log(guild: discord.Guild, text: str):
    vacation_log_channel = await get_id(guild.id, "vacation_log_channel")
    if not vacation_log_channel:
        return
    ch = guild.get_channel(vacation_log_channel)
    if ch:
        await ch.send(text, silent=True)


class VacationCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="vacation", description="Уйти в отпуск")
    @app_commands.describe(days="Количество дней (1-30)", reason="Причина отпуска")
    async def vacation_go(
        self,
        interaction: discord.Interaction,
        days: int,
        reason: str = "Личные дела",
    ):
        if not 1 <= days <= MAX_VACATION_DAYS:
            await interaction.response.send_message(
                f"⚠️ Укажи от 1 до {MAX_VACATION_DAYS} дней.", ephemeral=True
            )
            return

        async with aiosqlite.connect(DB) as db:
            async with db.execute(
                "SELECT id FROM vacations WHERE user_id=? AND status='active'",
                (interaction.user.id,),
            ) as cur:
                active = await cur.fetchone()

        if active:
            await interaction.response.send_message(
                "⚠️ Ты уже в отпуске. Сначала заверши его: `/vacation_end`", ephemeral=True
            )
            return

        end_date = datetime.utcnow() + timedelta(days=days)

        async with aiosqlite.connect(DB) as db:
            await db.execute(
                """INSERT INTO vacations (user_id, reason, days, end_date, status, started_at)
                   VALUES (?,?,?,?,'active',CURRENT_TIMESTAMP)""",
                (interaction.user.id, reason, days, end_date.isoformat()),
            )
            await db.commit()

        # Выдать роль
        vacation_role_id = await get_id(interaction.guild_id, "vacation_role_id")
        if vacation_role_id:
            role = interaction.guild.get_role(vacation_role_id)
            if role:
                try:
                    await interaction.user.add_roles(role, reason="Отпуск")
                except discord.Forbidden:
                    pass

        embed = discord.Embed(
            title="🏖️ Отпуск оформлен",
            description=(
                f"{interaction.user.mention} ушёл в отпуск!\n\n"
                f"📅 **Дней:** {days}\n"
                f"📝 **Причина:** {reason}\n"
                f"🔔 **Возвращение:** <t:{int(end_date.timestamp())}:D>"
            ),
            color=discord.Color.from_rgb(255, 165, 0),
            timestamp=datetime.utcnow(),
        )
        embed.set_footer(text="Используй /vacation_end чтобы вернуться раньше")
        await interaction.response.send_message(embed=embed)

        await _log(
            interaction.guild,
            f"🏖️ **{interaction.user.display_name}** ушёл в отпуск на **{days} дн.** Причина: *{reason}*",
        )

    @app_commands.command(name="vacation_end", description="Завершить отпуск досрочно")
    async def vacation_end(self, interaction: discord.Interaction):
        async with aiosqlite.connect(DB) as db:
            async with db.execute(
                "SELECT id, started_at FROM vacations WHERE user_id=? AND status='active'",
                (interaction.user.id,),
            ) as cur:
                row = await cur.fetchone()

        if not row:
            await interaction.response.send_message("⚠️ Ты не в отпуске.", ephemeral=True)
            return

        async with aiosqlite.connect(DB) as db:
            await db.execute(
                "UPDATE vacations SET status='ended', ended_at=CURRENT_TIMESTAMP WHERE id=?",
                (row[0],),
            )
            await db.commit()

        # Снять роль
        vacation_role_id = await get_id(interaction.guild_id, "vacation_role_id")
        if vacation_role_id:
            role = interaction.guild.get_role(vacation_role_id)
            if role and role in interaction.user.roles:
                try:
                    await interaction.user.remove_roles(role, reason="Отпуск завершён")
                except discord.Forbidden:
                    pass

        started = datetime.fromisoformat(row[1])
        days_spent = (datetime.utcnow() - started).days

        embed = discord.Embed(
            title="✅ Отпуск завершён",
            description=f"{interaction.user.mention} вернулся из отпуска!\nБыл в отпуске: **{days_spent} дн.**",
            color=discord.Color.green(),
        )
        await interaction.response.send_message(embed=embed)
        await _log(interaction.guild, f"✅ **{interaction.user.display_name}** вернулся из отпуска.")

    @app_commands.command(name="vacation_list", description="Список участников в отпуске")
    async def vacation_list(self, interaction: discord.Interaction):
        async with aiosqlite.connect(DB) as db:
            async with db.execute(
                "SELECT user_id, reason, days, end_date, started_at FROM vacations WHERE status='active' ORDER BY started_at ASC"
            ) as cur:
                rows = await cur.fetchall()

        if not rows:
            await interaction.response.send_message("✅ Никто не в отпуске.", ephemeral=True)
            return

        embed = discord.Embed(
            title="🏖️ Список отпусков",
            color=discord.Color.from_rgb(255, 165, 0),
            timestamp=datetime.utcnow(),
        )

        for uid, reason, days, end_date, started_at in rows:
            end_ts = int(datetime.fromisoformat(end_date).timestamp())
            embed.add_field(
                name=f"<@{uid}>",
                value=(
                    f"📝 **Причина:** {reason}\n"
                    f"📅 **Дней:** {days}\n"
                    f"🔔 **До:** <t:{end_ts}:D>"
                ),
                inline=False,
            )

        embed.set_footer(text=f"В отпуске: {len(rows)} чел.")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="vacation_remove", description="[HR] Завершить отпуск участника принудительно")
    @app_commands.describe(member="Участник")
    async def vacation_remove(self, interaction: discord.Interaction, member: discord.Member):
        if not await _is_hr(interaction):
            await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
            return

        async with aiosqlite.connect(DB) as db:
            async with db.execute(
                "SELECT id FROM vacations WHERE user_id=? AND status='active'", (member.id,)
            ) as cur:
                row = await cur.fetchone()

        if not row:
            await interaction.response.send_message(f"⚠️ {member.mention} не в отпуске.", ephemeral=True)
            return

        async with aiosqlite.connect(DB) as db:
            await db.execute(
                "UPDATE vacations SET status='ended', ended_at=CURRENT_TIMESTAMP WHERE id=?",
                (row[0],),
            )
            await db.commit()

        vacation_role_id = await get_id(interaction.guild_id, "vacation_role_id")
        if vacation_role_id:
            role = interaction.guild.get_role(vacation_role_id)
            if role and role in member.roles:
                try:
                    await member.remove_roles(role, reason="Отпуск завершён HR")
                except discord.Forbidden:
                    pass

        await interaction.response.send_message(
            f"✅ Отпуск {member.mention} завершён.", ephemeral=True
        )
        await _log(interaction.guild, f"✅ Отпуск **{member.display_name}** завершён принудительно ({interaction.user.display_name})")


async def setup(bot: commands.Bot):
    await bot.add_cog(VacationCog(bot))