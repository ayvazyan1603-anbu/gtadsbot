import discord
from discord.ext import commands, tasks
from discord import app_commands
import aiosqlite
from datetime import datetime, timedelta

from cogs.config import get_id, get_list

DB = "bot.db"

# ── CONFIG — настраивается командой /config:
#   voice_log_channel       — канал для логов голосовых сессий
#   excluded_voice_channels — голосовые каналы, которые НЕ считаются (АФК-канал и т.д.)
#   hr_role_id              — роль HR / Администрации для просмотра статистики других
# ───────────────────────────────────────────────

def fmt_duration(seconds: int) -> str:
    """Форматирует секунды → '1д 2ч 15м 30с'."""
    if seconds <= 0:
        return "0с"
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s   = divmod(rem, 60)
    parts = []
    if d: parts.append(f"{d}д")
    if h: parts.append(f"{h}ч")
    if m: parts.append(f"{m}м")
    if s: parts.append(f"{s}с")
    return " ".join(parts)


class VoiceTracker(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # user_id → join timestamp (datetime)
        self._sessions: dict[int, datetime] = {}
        self.save_active_sessions.start()

    def cog_unload(self):
        self.save_active_sessions.cancel()

    # ── Events ────────────────────────────────

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        uid = member.id
        excluded = await get_list(member.guild.id, "excluded_voice_channels")

        # Вошёл в голосовой канал
        joined = after.channel and after.channel.id not in excluded
        left   = before.channel and before.channel.id not in excluded

        if joined and not left:
            # Пришёл в войс
            self._sessions[uid] = datetime.utcnow()
            await self._log_join(member, after.channel)

        elif left and not joined:
            # Вышел из войса
            await self._save_session(uid, before.channel)

        elif left and joined and before.channel != after.channel:
            # Переключился между каналами — закрываем старую сессию, открываем новую
            await self._save_session(uid, before.channel)
            self._sessions[uid] = datetime.utcnow()

    async def _save_session(self, user_id: int, channel: discord.VoiceChannel | None):
        join_time = self._sessions.pop(user_id, None)
        if join_time is None:
            return

        duration = int((datetime.utcnow() - join_time).total_seconds())
        if duration < 1:
            return

        async with aiosqlite.connect(DB) as db:
            await db.execute(
                """INSERT INTO voice_sessions (user_id, channel_id, joined_at, left_at, duration_seconds)
                   VALUES (?, ?, ?, CURRENT_TIMESTAMP, ?)""",
                (user_id, channel.id if channel else 0, join_time.isoformat(), duration),
            )
            await db.execute(
                """INSERT INTO voice_totals (user_id, total_seconds)
                   VALUES (?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                       total_seconds = total_seconds + excluded.total_seconds,
                       last_updated = CURRENT_TIMESTAMP""",
                (user_id, duration),
            )
            await db.commit()

    async def _log_join(self, member: discord.Member, channel: discord.VoiceChannel):
        voice_log_channel = await get_id(member.guild.id, "voice_log_channel")
        if not voice_log_channel:
            return
        log_ch = member.guild.get_channel(voice_log_channel)
        if log_ch:
            await log_ch.send(
                f"🎙️ **{member.display_name}** вошёл в **{channel.name}** "
                f"<t:{int(datetime.utcnow().timestamp())}:T>",
                silent=True,
            )

    # ── Периодическое сохранение активных сессий ──

    @tasks.loop(minutes=5)
    async def save_active_sessions(self):
        """Каждые 5 минут пишем промежуточное время — чтобы не потерять при крэше."""
        now = datetime.utcnow()
        async with aiosqlite.connect(DB) as db:
            for uid, join_time in list(self._sessions.items()):
                elapsed = int((now - join_time).total_seconds())
                if elapsed < 60:
                    continue
                await db.execute(
                    """INSERT INTO voice_totals (user_id, total_seconds)
                       VALUES (?, ?)
                       ON CONFLICT(user_id) DO UPDATE SET
                           total_seconds = total_seconds + excluded.total_seconds,
                           last_updated = CURRENT_TIMESTAMP""",
                    (uid, elapsed),
                )
                self._sessions[uid] = now  # сбрасываем точку отсчёта
            await db.commit()

    @save_active_sessions.before_loop
    async def before_save(self):
        await self.bot.wait_until_ready()

    # ── Slash commands ─────────────────────────

    @app_commands.command(name="voice_stats", description="Сколько часов ты провёл в голосовых каналах")
    @app_commands.describe(member="Участник (только для HR)")
    async def voice_stats(
        self, interaction: discord.Interaction, member: discord.Member | None = None
    ):
        # Если запрашивают другого — проверяем права
        target = member or interaction.user
        if member and member != interaction.user:
            role_ids = {r.id for r in interaction.user.roles}
            hr_role_id = await get_id(interaction.guild_id, "hr_role_id")
            if (not hr_role_id or hr_role_id not in role_ids) and not interaction.user.guild_permissions.administrator:
                await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
                return

        async with aiosqlite.connect(DB) as db:
            async with db.execute(
                "SELECT total_seconds FROM voice_totals WHERE user_id=?", (target.id,)
            ) as cur:
                row = await cur.fetchone()

            # Добавляем текущую сессию, если есть
            extra = 0
            if target.id in self._sessions:
                extra = int((datetime.utcnow() - self._sessions[target.id]).total_seconds())

        total = (row[0] if row else 0) + extra

        # Топ сессий
        async with aiosqlite.connect(DB) as db:
            async with db.execute(
                """SELECT duration_seconds, joined_at FROM voice_sessions
                   WHERE user_id=? ORDER BY duration_seconds DESC LIMIT 3""",
                (target.id,),
            ) as cur:
                top_sessions = await cur.fetchall()

        embed = discord.Embed(
            title=f"🎙️ Голосовая статистика — {target.display_name}",
            color=discord.Color.purple(),
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(name="⏱️ Всего в войсе", value=f"**{fmt_duration(total)}**", inline=False)

        if extra > 0:
            embed.add_field(
                name="🟢 Текущая сессия",
                value=fmt_duration(extra),
                inline=True,
            )

        if top_sessions:
            top_text = "\n".join(
                f"`{i+1}.` {fmt_duration(s)} — <t:{int(datetime.fromisoformat(d).timestamp())}:d>"
                for i, (s, d) in enumerate(top_sessions)
            )
            embed.add_field(name="🏆 Топ сессий", value=top_text, inline=False)

        embed.set_footer(text="ETERNAL HELPER • Голосовой трекер")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="voice_top", description="Топ участников по времени в войсе")
    @app_commands.describe(limit="Кол-во мест (по умолч. 10)")
    async def voice_top(self, interaction: discord.Interaction, limit: int = 10):
        limit = max(1, min(limit, 25))

        async with aiosqlite.connect(DB) as db:
            async with db.execute(
                "SELECT user_id, total_seconds FROM voice_totals ORDER BY total_seconds DESC LIMIT ?",
                (limit,),
            ) as cur:
                rows = await cur.fetchall()

        if not rows:
            await interaction.response.send_message("📭 Нет данных.", ephemeral=True)
            return

        lines = []
        medals = ["🥇", "🥈", "🥉"]
        for i, (uid, secs) in enumerate(rows):
            # Добавляем активную сессию
            extra = 0
            if uid in self._sessions:
                extra = int((datetime.utcnow() - self._sessions[uid]).total_seconds())
            icon = medals[i] if i < 3 else f"`{i+1}.`"
            lines.append(f"{icon} <@{uid}> — **{fmt_duration(secs + extra)}**")

        embed = discord.Embed(
            title="🏆 Топ по времени в голосовых каналах",
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        embed.set_footer(text="ETERNAL HELPER • Голосовой трекер")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="voice_reset", description="[ADMIN] Сбросить время участника")
    @app_commands.describe(member="Участник")
    @app_commands.checks.has_permissions(administrator=True)
    async def voice_reset(self, interaction: discord.Interaction, member: discord.Member):
        async with aiosqlite.connect(DB) as db:
            await db.execute("DELETE FROM voice_totals WHERE user_id=?", (member.id,))
            await db.execute("DELETE FROM voice_sessions WHERE user_id=?", (member.id,))
            await db.commit()
        self._sessions.pop(member.id, None)
        await interaction.response.send_message(
            f"✅ Голосовая статистика **{member.display_name}** сброшена.", ephemeral=True
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(VoiceTracker(bot))
