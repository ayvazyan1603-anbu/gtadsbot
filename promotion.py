import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite
from datetime import datetime

from cogs.config import get_id

DB = "bot.db"

# ── CONFIG ─────────────────────────────────────
# Все ID настраиваются командами /config:
#   afk_role_id      — роль, которая выдаётся при АФК
#   afk_log_channel  — канал для логов АФК
#   hr_role_id       — роль HR для управления АФК других
MAX_AFK_REASON    = 200  # Макс символов в причине
# ───────────────────────────────────────────────


class AfkCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _is_hr(self, interaction: discord.Interaction) -> bool:
        if interaction.user.guild_permissions.administrator:
            return True
        role_ids = {r.id for r in interaction.user.roles}
        hr_role_id = await get_id(interaction.guild_id, "hr_role_id")
        return bool(hr_role_id) and hr_role_id in role_ids

    async def _set_afk(self, guild: discord.Guild, member: discord.Member, reason: str):
        """Ставит участника в АФК."""
        async with aiosqlite.connect(DB) as db:
            await db.execute(
                "INSERT OR REPLACE INTO afk_list (user_id, reason, started_at) VALUES (?,?,CURRENT_TIMESTAMP)",
                (member.id, reason[:MAX_AFK_REASON]),
            )
            await db.commit()

        # Выдать роль АФК
        afk_role_id = await get_id(guild.id, "afk_role_id")
        if afk_role_id:
            role = guild.get_role(afk_role_id)
            if role:
                try:
                    await member.add_roles(role, reason="АФК")
                except discord.Forbidden:
                    pass

        # Лог
        await self._log(guild, f"😴 **{member.display_name}** ушёл в АФК. Причина: *{reason}*")

    async def _remove_afk(self, guild: discord.Guild, member: discord.Member) -> bool:
        """Убирает участника из АФК. Возвращает True если был в АФК."""
        async with aiosqlite.connect(DB) as db:
            async with db.execute(
                "SELECT started_at FROM afk_list WHERE user_id=?", (member.id,)
            ) as cur:
                row = await cur.fetchone()
            if not row:
                return False
            await db.execute("DELETE FROM afk_list WHERE user_id=?", (member.id,))
            await db.commit()

        # Снять роль
        afk_role_id = await get_id(guild.id, "afk_role_id")
        if afk_role_id:
            role = guild.get_role(afk_role_id)
            if role and role in member.roles:
                try:
                    await member.remove_roles(role, reason="Вернулся из АФК")
                except discord.Forbidden:
                    pass

        # Считаем продолжительность
        started = datetime.fromisoformat(row[0])
        duration_s = int((datetime.utcnow() - started).total_seconds())
        h, rem = divmod(duration_s, 3600)
        m, s   = divmod(rem, 60)
        dur_str = f"{h}ч {m}м" if h else f"{m}м {s}с"

        await self._log(
            guild,
            f"✅ **{member.display_name}** вернулся из АФК. Был в АФК: **{dur_str}**",
        )
        return True

    async def _log(self, guild: discord.Guild, text: str):
        afk_log_channel = await get_id(guild.id, "afk_log_channel")
        if not afk_log_channel:
            return
        ch = guild.get_channel(afk_log_channel)
        if ch:
            await ch.send(text, silent=True)

    # ── Slash commands ─────────────────────────

    @app_commands.command(name="afk", description="Уйти в АФК")
    @app_commands.describe(reason="Причина (необязательно)")
    async def afk_go(self, interaction: discord.Interaction, reason: str = "Нет причины"):
        # Уже в АФК?
        async with aiosqlite.connect(DB) as db:
            async with db.execute(
                "SELECT 1 FROM afk_list WHERE user_id=?", (interaction.user.id,)
            ) as cur:
                already = await cur.fetchone()

        if already:
            await interaction.response.send_message(
                "⚠️ Ты уже в АФК. Сначала выйди: `/afk_back`", ephemeral=True
            )
            return

        await self._set_afk(interaction.guild, interaction.user, reason)

        embed = discord.Embed(
            title="😴 АФК",
            description=f"{interaction.user.mention} ушёл в АФК.\n**Причина:** {reason}",
            color=discord.Color.light_grey(),
            timestamp=datetime.utcnow(),
        )
        embed.set_footer(text="Чтобы вернуться, используй /afk_back")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="afk_back", description="Вернуться из АФК")
    async def afk_back(self, interaction: discord.Interaction):
        was_afk = await self._remove_afk(interaction.guild, interaction.user)

        if not was_afk:
            await interaction.response.send_message(
                "⚠️ Ты не в АФК.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title="✅ Добро пожаловать обратно!",
            description=f"{interaction.user.mention} вернулся из АФК.",
            color=discord.Color.green(),
            timestamp=datetime.utcnow(),
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="afk_list", description="Список участников в АФК")
    async def afk_list_cmd(self, interaction: discord.Interaction):
        async with aiosqlite.connect(DB) as db:
            async with db.execute(
                "SELECT user_id, reason, started_at FROM afk_list ORDER BY started_at ASC"
            ) as cur:
                rows = await cur.fetchall()

        if not rows:
            await interaction.response.send_message(
                "✅ Сейчас никто не в АФК.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title="😴 Список АФК",
            color=discord.Color.light_grey(),
            timestamp=datetime.utcnow(),
        )

        for user_id, reason, started_at in rows:
            started = datetime.fromisoformat(started_at)
            duration_s = int((datetime.utcnow() - started).total_seconds())
            h, rem = divmod(duration_s, 3600)
            m, _   = divmod(rem, 60)
            dur_str = f"{h}ч {m}м" if h else f"{m}м"

            embed.add_field(
                name=f"<@{user_id}>",
                value=(
                    f"📝 **Причина:** {reason}\n"
                    f"⏱️ **С:** <t:{int(started.timestamp())}:R> ({dur_str})"
                ),
                inline=False,
            )

        embed.set_footer(text=f"Всего в АФК: {len(rows)}")
        await interaction.response.send_message(embed=embed)

    # ── HR-команды ─────────────────────────────

    @app_commands.command(name="afk_set", description="[HR] Поставить участника в АФК")
    @app_commands.describe(member="Участник", reason="Причина")
    async def afk_set(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str = "Принудительный АФК",
    ):
        if not await self._is_hr(interaction):
            await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
            return

        async with aiosqlite.connect(DB) as db:
            async with db.execute(
                "SELECT 1 FROM afk_list WHERE user_id=?", (member.id,)
            ) as cur:
                already = await cur.fetchone()

        if already:
            await interaction.response.send_message(
                f"⚠️ {member.mention} уже в АФК.", ephemeral=True
            )
            return

        await self._set_afk(interaction.guild, member, reason)
        await interaction.response.send_message(
            f"✅ {member.mention} поставлен в АФК. Причина: *{reason}*", ephemeral=True
        )

    @app_commands.command(name="afk_remove", description="[HR] Вывести участника из АФК")
    @app_commands.describe(member="Участник")
    async def afk_remove(self, interaction: discord.Interaction, member: discord.Member):
        if not await self._is_hr(interaction):
            await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
            return

        was_afk = await self._remove_afk(interaction.guild, member)
        if not was_afk:
            await interaction.response.send_message(
                f"⚠️ {member.mention} не в АФК.", ephemeral=True
            )
            return

        await interaction.response.send_message(
            f"✅ {member.mention} выведен из АФК.", ephemeral=True
        )

    # ── Auto-return при написании сообщения ────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        async with aiosqlite.connect(DB) as db:
            async with db.execute(
                "SELECT 1 FROM afk_list WHERE user_id=?", (message.author.id,)
            ) as cur:
                in_afk = await cur.fetchone()

        if in_afk:
            await self._remove_afk(message.guild, message.author)
            try:
                await message.channel.send(
                    f"👋 {message.author.mention}, добро пожаловать обратно! АФК снят.",
                    delete_after=5,
                )
            except discord.Forbidden:
                pass


async def setup(bot: commands.Bot):
    await bot.add_cog(AfkCog(bot))
