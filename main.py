# -*- coding: utf-8 -*-
import asyncio
import colorsys
import html
import io
import os
import platform
import socket
import time
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

import psutil
from PIL import Image
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.config.astrbot_config import AstrBotConfig


PLUGIN_NAME = "astrbot_plugin_status_card"


@dataclass
class NetworkPoint:
    ts: float
    sent_bps: float
    recv_bps: float


class StatusCardPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}
        self.started_at = time.time()
        self._network_points: deque[NetworkPoint] = deque()
        self._sampler_task: asyncio.Task | None = None
        self._last_net: tuple[float, int, int] | None = None
        self._llm_requests = 0
        self._llm_errors = 0
        self._input_tokens = 0
        self._output_tokens = 0
        self._cache_hits: int | None = None
        self._tool_counter: Counter[str] = Counter()

    async def initialize(self):
        self._sampler_task = asyncio.create_task(self._sample_network_loop())
        logger.info("[StatusCard] loaded")

    async def terminate(self):
        if self._sampler_task:
            self._sampler_task.cancel()
            try:
                await self._sampler_task
            except asyncio.CancelledError:
                pass
        logger.info("[StatusCard] terminated")

    def _cfg(self, key: str, default: Any = None) -> Any:
        if hasattr(self.config, "get"):
            return self.config.get(key, default)
        return default

    @filter.command("状态", alias=["status"])
    async def status_card(self, event: AstrMessageEvent):
        """生成机器人状态图。"""
        try:
            data = await self._collect_status_data(event)
            image = await self.html_render(
                self._template(),
                data,
                return_url=False,
                options={
                    "full_page": True,
                    "type": "png",
                    "timeout": 30000,
                    "animations": "disabled",
                    "caret": "hide",
                    "scale": "css",
                },
            )
            yield event.image_result(self._crop_rendered_image(image))
        except Exception as exc:
            logger.error(f"[StatusCard] render failed: {exc}", exc_info=True)
            yield event.plain_result("状态图生成失败，请查看 AstrBot 日志。")

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, request: Any, *args, **kwargs) -> None:
        self._llm_requests += 1
        self._input_tokens += self._extract_token_count(
            request,
            ("prompt_tokens", "input_tokens", "total_prompt_tokens"),
        )

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, response: Any, *args, **kwargs) -> None:
        if self._looks_like_error(response):
            self._llm_errors += 1
        self._output_tokens += self._extract_token_count(
            response,
            ("completion_tokens", "output_tokens", "total_completion_tokens"),
        )
        cache_hit = self._extract_optional_int(response, ("cache_hit", "cache_hits"))
        if cache_hit is not None:
            self._cache_hits = (self._cache_hits or 0) + cache_hit

    @filter.on_using_llm_tool()
    async def on_using_llm_tool(
        self,
        event: AstrMessageEvent,
        tool: Any,
        tool_args: dict | None = None,
        *args,
        **kwargs,
    ) -> None:
        name = str(getattr(tool, "name", "") or getattr(tool, "__name__", "") or tool.__class__.__name__)
        if name:
            self._tool_counter[name] += 1

    async def _sample_network_loop(self) -> None:
        interval = max(float(self._cfg("network_sample_interval_seconds", 5)), 1.0)
        while True:
            try:
                self._sample_network_once()
            except Exception as exc:
                logger.debug(f"[StatusCard] network sample failed: {exc}")
            await asyncio.sleep(interval)

    def _sample_network_once(self) -> None:
        counters = psutil.net_io_counters()
        now = time.time()
        if self._last_net is not None:
            last_ts, last_sent, last_recv = self._last_net
            delta = max(now - last_ts, 0.001)
            sent_bps = max((counters.bytes_sent - last_sent) / delta, 0.0)
            recv_bps = max((counters.bytes_recv - last_recv) / delta, 0.0)
            self._network_points.append(NetworkPoint(now, sent_bps, recv_bps))

        self._last_net = (now, counters.bytes_sent, counters.bytes_recv)
        window = max(float(self._cfg("network_window_minutes", 30)), 1.0) * 60
        while self._network_points and now - self._network_points[0].ts > window:
            self._network_points.popleft()

    async def _collect_status_data(self, event: AstrMessageEvent) -> dict[str, Any]:
        bot = await self._collect_bot_info(event)
        system = self._collect_system_info()
        platform_info = await self._collect_platform_info(event)
        session = await self._collect_session_info(event)
        dashboard = await self._collect_dashboard_stats(session)

        return {
            "title": self._safe(self._cfg("display_title", "MIOKU STATUS")),
            "bot": bot,
            "system": system,
            "dashboard": dashboard,
            "platform": platform_info,
            "session": session,
            "show_model_stats": bool(self._cfg("show_model_stats", True)),
            "show_message_stats": bool(self._cfg("show_message_stats", True)),
            "background_style": self._background_style(bot.get("avatar_raw", "")),
            "theme_style": self._theme_style(bot.get("avatar_raw", "")),
        }

    async def _collect_bot_info(self, event: AstrMessageEvent) -> dict[str, Any]:
        fallback_name = str(self._cfg("fallback_bot_name", "AstrBot"))
        bot_id = self._call_event(event, "get_self_id") or "-"
        name = fallback_name
        avatar = self._config_file_uri("avatar_file")

        client = await self._get_client(event)
        if client is not None:
            login = await self._call_client(client, "get_login_info")
            if isinstance(login, dict):
                bot_id = str(login.get("user_id") or bot_id or "-")
                name = str(login.get("nickname") or login.get("name") or name)
                avatar = avatar or self._avatar_url(bot_id)

        if not avatar and bot_id and bot_id != "-":
            avatar = self._avatar_url(bot_id)

        status = self._call_event(event, "get_platform_name") or "-"
        return {
            "name": self._safe(name),
            "id": self._safe(bot_id),
            "avatar": self._safe(avatar),
            "avatar_raw": avatar,
            "status": self._safe(status),
        }

    async def _collect_platform_info(self, event: AstrMessageEvent) -> dict[str, str]:
        client = await self._get_client(event)
        friends = "-"
        groups = "-"
        if client is not None:
            friend_list = await self._call_client(client, "get_friend_list")
            group_list = await self._call_client(client, "get_group_list")
            if isinstance(friend_list, list):
                friends = str(len(friend_list))
            if isinstance(group_list, list):
                groups = str(len(group_list))
        return {
            "friends": self._safe(friends),
            "groups": self._safe(groups),
        }

    async def _collect_dashboard_stats(self, session: dict[str, str] | None = None) -> dict[str, Any]:
        db_helper = self._find_db_helper()
        base = await self._collect_base_stats_official(db_helper, 86400)
        provider = await self._collect_provider_stats_official(db_helper, 1)
        session = session or {}

        platform_count = base.get("platform_count")
        message_total = base.get("message_count")
        model_tokens = provider.get("today_total_tokens")
        model_calls = provider.get("range_total_calls")
        success_rate = provider.get("range_success_rate")
        message_rank = self._rank_platform_stats(base.get("platform", []))
        message_chart = self._build_message_chart(base.get("message_time_series", []))
        session_rank = self._rank_token_items(
            [
                {"name": item.get("umo", "unknown"), "count": int(item.get("tokens", 0) or 0)}
                for item in provider.get("range_by_umo", [])[:10]
                if isinstance(item, dict)
            ]
        )
        model_chart = self._build_model_bar_chart(provider.get("trend", {}).get("model_series", []))
        model_daily_rank = self._rank_token_items(
            [
                {"name": item.get("provider_model", "Unknown"), "count": int(item.get("tokens", 0) or 0)}
                for item in provider.get("today_by_model", [])[:6]
                if isinstance(item, dict)
            ]
        )
        memory = base.get("memory", {}) if isinstance(base.get("memory"), dict) else {}

        return {
            "cards": [
                {"label": "平台实例数", "value": self._display_number(platform_count), "sub": "当前已加载平台"},
                {"label": "今日模型调用", "value": self._fmt_count(int(model_tokens or 0)) if model_tokens else "-", "sub": "词元 Tokens"},
                {
                    "label": "会话 Token",
                    "value": session.get("token_total", "-"),
                    "sub": f"输入 {session.get('token_input', '-')} / 输出 {session.get('token_output', '-')}",
                },
                {"label": "插件数量", "value": self._display_number(base.get("plugin_count")), "sub": "当前已启用插件"},
            ],
            "message_total": self._display_number(message_total),
            "message_rank": message_rank,
            "message_chart": message_chart,
            "model_total_tokens": self._display_number(provider.get("range_total_tokens")),
            "model_calls": self._display_number(model_calls),
            "model_success_rate": f"{success_rate * 100:.1f}%" if isinstance(success_rate, (int, float)) else "-",
            "model_chart": model_chart,
            "model_daily_rank": model_daily_rank,
            "session_rank": session_rank,
            "fallback_ai": self._ai_stats(),
        }

    async def _collect_session_info(self, event: AstrMessageEvent) -> dict[str, str]:
        session_id = str(getattr(event, "unified_msg_origin", "") or "")
        provider = self._get_using_provider(session_id)
        provider_id = "-"
        model_name = "-"
        if provider is not None:
            try:
                meta = provider.meta()
                provider_id = str(getattr(meta, "id", None) or "-")
                model_name = str(getattr(meta, "model", None) or provider_id or "-")
            except Exception:
                provider_id = str(getattr(provider, "id", None) or "-")
                model_name = str(getattr(provider, "model", None) or provider_id or "-")

        persona_name = await self._get_current_persona_name(event, session_id)
        token_stats = await self._collect_session_token_stats(session_id)

        return {
            "model": self._safe(model_name),
            "provider": self._safe(provider_id),
            "persona": self._safe(persona_name),
            "token_total": self._safe(token_stats["total"]),
            "token_input": self._safe(token_stats["input"]),
            "token_output": self._safe(token_stats["output"]),
        }

    async def _collect_session_token_stats(self, session_id: str) -> dict[str, str]:
        empty = {"total": "-", "input": "-", "output": "-"}
        conversation_id = await self._get_current_conversation_id(session_id)
        if not conversation_id:
            return empty
        db_helper = self._find_db_helper()
        if db_helper is None:
            return empty

        try:
            from sqlmodel import case, col, func, select
            from astrbot.core.db.po import ProviderStat
        except Exception as exc:
            logger.debug(f"[StatusCard] ProviderStat token query import failed: {exc}")
            return empty

        try:
            async with db_helper.get_db() as session:
                result = await session.execute(
                    select(
                        func.count(case((col(ProviderStat.id).is_not(None), 1))).label("record_count"),
                        func.coalesce(func.sum(ProviderStat.token_input_other), 0).label("total_input_other"),
                        func.coalesce(func.sum(ProviderStat.token_input_cached), 0).label("total_input_cached"),
                        func.coalesce(func.sum(ProviderStat.token_output), 0).label("total_output"),
                    ).where(
                        col(ProviderStat.agent_type) == "internal",
                        col(ProviderStat.conversation_id) == conversation_id,
                    )
                )
                stats = result.one()
        except Exception as exc:
            logger.debug(f"[StatusCard] session token stats unavailable: {exc}")
            return empty

        record_count = int(getattr(stats, "record_count", 0) or 0)
        if record_count <= 0:
            return empty
        input_cached = int(getattr(stats, "total_input_cached", 0) or 0)
        input_other = int(getattr(stats, "total_input_other", 0) or 0)
        output = int(getattr(stats, "total_output", 0) or 0)
        input_total = input_cached + input_other
        total = input_total + output
        return {
            "total": self._fmt_count(total),
            "input": self._fmt_count(input_total),
            "output": self._fmt_count(output),
        }

    def _get_using_provider(self, session_id: str) -> Any:
        getter = getattr(self.context, "get_using_provider", None)
        if callable(getter):
            try:
                return getter(umo=session_id)
            except TypeError:
                try:
                    return getter(session_id)
                except Exception:
                    return None
            except Exception as exc:
                logger.debug(f"[StatusCard] current provider unavailable: {exc}")
        return None

    async def _get_current_persona_name(self, event: AstrMessageEvent, session_id: str) -> str:
        if not session_id:
            return "-"

        cfg = self._get_session_config(session_id)
        provider_settings = cfg.get("provider_settings", {}) if isinstance(cfg, dict) else {}
        conversation_persona_id = None
        conv_mgr = getattr(self.context, "conversation_manager", None)
        curr = await self._get_current_conversation_id(session_id)
        if conv_mgr is not None and curr:
            try:
                conv = await self._maybe_await(
                    self._call_any(conv_mgr, "get_conversation", session_id, curr)
                )
                conversation_persona_id = getattr(conv, "persona_id", None) if conv is not None else None
            except Exception as exc:
                logger.debug(f"[StatusCard] current conversation persona unavailable: {exc}")

        persona_mgr = getattr(self.context, "persona_manager", None)
        platform_name = self._call_event(event, "get_platform_name") or ""
        if persona_mgr is not None:
            resolver = getattr(persona_mgr, "resolve_selected_persona", None)
            if callable(resolver):
                try:
                    resolved = await self._maybe_await(
                        resolver(
                            umo=session_id,
                            conversation_persona_id=conversation_persona_id,
                            platform_name=platform_name,
                            provider_settings=provider_settings,
                        )
                    )
                    if isinstance(resolved, tuple) and resolved:
                        persona_id = resolved[0]
                        persona = resolved[1] if len(resolved) > 1 else None
                        return self._persona_display_name(persona, persona_id)
                except Exception as exc:
                    logger.debug(f"[StatusCard] resolved persona unavailable: {exc}")

            getter = getattr(persona_mgr, "get_persona_v3_by_id", None)
            if callable(getter) and conversation_persona_id:
                try:
                    persona = getter(conversation_persona_id)
                    return self._persona_display_name(persona, conversation_persona_id)
                except Exception:
                    pass

        default_persona = provider_settings.get("default_personality") or "default"
        return str(conversation_persona_id or default_persona or "-")

    async def _get_current_conversation_id(self, session_id: str) -> str | None:
        if not session_id:
            return None
        conv_mgr = getattr(self.context, "conversation_manager", None)
        if conv_mgr is None:
            return None
        try:
            curr = await self._maybe_await(
                self._call_any(conv_mgr, "get_curr_conversation_id", session_id)
            )
            return str(curr) if curr else None
        except Exception as exc:
            logger.debug(f"[StatusCard] current conversation id unavailable: {exc}")
            return None

    def _get_session_config(self, session_id: str) -> dict[str, Any]:
        getter = getattr(self.context, "get_config", None)
        if callable(getter):
            try:
                cfg = getter(umo=session_id)
            except TypeError:
                try:
                    cfg = getter(session_id)
                except Exception:
                    cfg = {}
            except Exception:
                cfg = {}
            if hasattr(cfg, "get"):
                return cfg
        return {}

    def _persona_display_name(self, persona: Any, persona_id: Any) -> str:
        if persona_id == "[%None]":
            return "无人格"
        if isinstance(persona, dict):
            return str(persona.get("name") or persona_id or "-")
        name = getattr(persona, "name", None) or getattr(persona, "persona_id", None)
        return str(name or persona_id or "-")

    async def _get_client(self, event: AstrMessageEvent) -> Any:
        client = getattr(event, "bot", None)
        if client:
            return client
        try:
            platform_name = event.get_platform_name()
            platform_inst = self.context.get_platform(platform_name)
            getter = getattr(platform_inst, "get_client", None)
            if callable(getter):
                return getter()
        except Exception:
            return None
        return None

    async def _call_client(self, client: Any, action: str) -> Any:
        try:
            call_action = getattr(client, "call_action", None)
            if callable(call_action):
                return await call_action(action)
        except Exception as exc:
            logger.debug(f"[StatusCard] client action {action} failed: {exc}")
        return None

    def _collect_system_info(self) -> dict[str, Any]:
        cpu_percent = psutil.cpu_percent(interval=0.2)
        vm = psutil.virtual_memory()
        process = psutil.Process(os.getpid())
        process_rss = process.memory_info().rss
        cpu_freq = psutil.cpu_freq()
        cpu_name = platform.processor() or platform.machine() or "-"
        process_percent = (process_rss / vm.total * 100) if vm.total else 0

        return {
            "cpu": {
                "percent": round(cpu_percent, 1),
                "text": f"{psutil.cpu_count(logical=False) or psutil.cpu_count() or '-'} 核 · {int(cpu_freq.current) if cpu_freq else '-'} MHz",
                "sub": self._safe(cpu_name),
            },
            "memory": {
                "percent": round(vm.percent, 1),
                "text": f"{self._fmt_bytes(vm.used)} / {self._fmt_bytes(vm.total)}",
                "sub": f"可用 {self._fmt_bytes(vm.available)}",
            },
            "process_memory": {
                "percent": round(process_percent, 1),
                "text": self._fmt_bytes(process_rss),
                "sub": f"系统内存 {self._fmt_bytes(vm.total)}",
            },
            "rows": [
                ("OS", f"{platform.system()} {platform.release()} ({platform.machine()})"),
                ("内核", platform.version()),
                ("处理器", cpu_name),
                ("主机", socket.gethostname()),
                ("Python", platform.python_version()),
            ],
        }

    def _collect_disk_info(self) -> list[dict[str, Any]]:
        disks = []
        for part in psutil.disk_partitions(all=False):
            try:
                usage = psutil.disk_usage(part.mountpoint)
            except Exception:
                continue
            disks.append(
                {
                    "name": self._safe(part.mountpoint),
                    "used": self._fmt_bytes(usage.used),
                    "total": self._fmt_bytes(usage.total),
                    "percent": round(usage.percent, 1),
                    "warn": usage.percent >= 75,
                }
            )
            if len(disks) >= 6:
                break
        return disks

    def _build_network_chart(self) -> dict[str, Any]:
        points = list(self._network_points)
        if not points:
            return {
                "up": "-",
                "down": "-",
                "total_up": "-",
                "total_down": "-",
                "svg_path_up": "",
                "svg_path_down": "",
                "labels": ["-30m", "-20m", "-10m", "now"],
            }

        max_value = max(max(p.sent_bps, p.recv_bps) for p in points) or 1
        width = 640
        height = 112
        up_path = self._line_path([p.sent_bps for p in points], max_value, width, height)
        down_path = self._line_path([p.recv_bps for p in points], max_value, width, height)
        latest = points[-1]
        return {
            "up": self._fmt_rate(latest.sent_bps),
            "down": self._fmt_rate(latest.recv_bps),
            "total_up": self._fmt_bytes(sum(p.sent_bps for p in points) * self._sample_interval_guess()),
            "total_down": self._fmt_bytes(sum(p.recv_bps for p in points) * self._sample_interval_guess()),
            "svg_path_up": up_path,
            "svg_path_down": down_path,
            "labels": ["-30m", "-20m", "-10m", "now"],
        }

    def _build_message_chart(self, series: Any) -> dict[str, Any]:
        points = series if isinstance(series, list) else []
        values: list[int] = []
        for point in points[-24:]:
            if isinstance(point, (list, tuple)) and len(point) >= 2:
                try:
                    values.append(int(point[1] or 0))
                except Exception:
                    values.append(0)
        if not values:
            values = [0] * 24
        if len(values) < 24:
            values = [0] * (24 - len(values)) + values

        max_value = max(values) or 1
        width = 520
        height = 128
        return {
            "path": self._line_path([float(v) for v in values], float(max_value), width, height),
            "area_path": self._area_path([float(v) for v in values], float(max_value), width, height),
            "max": self._display_number(max_value),
            "latest": self._display_number(values[-1] if values else 0),
            "labels": ["24h", "18h", "12h", "6h", "now"],
        }

    def _build_token_chart(self, series: Any) -> dict[str, Any]:
        points = series if isinstance(series, list) else []
        values: list[int] = []
        for point in points[-24:]:
            if isinstance(point, (list, tuple)) and len(point) >= 2:
                try:
                    values.append(int(point[1] or 0))
                except Exception:
                    values.append(0)
        if not values:
            values = [0] * 24
        if len(values) < 24:
            values = [0] * (24 - len(values)) + values

        max_value = max(values) or 1
        width = 820
        height = 142
        return {
            "path": self._line_path([float(v) for v in values], float(max_value), width, height),
            "area_path": self._area_path([float(v) for v in values], float(max_value), width, height),
            "max": self._display_number(max_value),
            "latest": self._display_number(values[-1] if values else 0),
            "labels": ["24h", "18h", "12h", "6h", "now"],
        }

    def _build_model_bar_chart(self, series: Any) -> dict[str, Any]:
        model_series = series if isinstance(series, list) else []
        colors = ["#3b847d", "#6da1b5", "#c9a56f", "#4a9b74", "#8fcac3", "#7f8fb5"]
        selected = [item for item in model_series[:6] if isinstance(item, dict)]
        buckets = 24
        model_values: list[dict[str, Any]] = []
        for idx, item in enumerate(selected):
            data = item.get("data", []) if isinstance(item.get("data", []), list) else []
            values: list[int] = []
            for point in data[-buckets:]:
                if isinstance(point, (list, tuple)) and len(point) >= 2:
                    try:
                        values.append(int(point[1] or 0))
                    except Exception:
                        values.append(0)
            if len(values) < buckets:
                values = [0] * (buckets - len(values)) + values
            model_values.append(
                {
                    "name": self._safe(item.get("name", "Unknown")),
                    "values": values,
                    "color": colors[idx % len(colors)],
                }
            )

        totals = [sum(model["values"][idx] for model in model_values) for idx in range(buckets)] if model_values else [0] * buckets
        max_total = max(totals) or 1
        width = 820
        height = 142
        left_pad = 8
        bar_gap = 5
        bar_width = (width - left_pad * 2 - bar_gap * (buckets - 1)) / buckets
        bars: list[dict[str, Any]] = []
        for bucket_idx in range(buckets):
            x = round(left_pad + bucket_idx * (bar_width + bar_gap), 2)
            y_cursor = height - 8
            segments = []
            for model in model_values:
                value = model["values"][bucket_idx]
                if value <= 0:
                    continue
                segment_height = max(2, (value / max_total) * (height - 18))
                y_cursor -= segment_height
                segments.append(
                    {
                        "x": x,
                        "y": round(y_cursor, 2),
                        "width": round(bar_width, 2),
                        "height": round(segment_height, 2),
                        "color": model["color"],
                    }
                )
            bars.append({"segments": segments})

        return {
            "bars": bars,
            "legend": [{"name": model["name"], "color": model["color"]} for model in model_values],
            "max": self._display_number(max_total),
            "latest": self._display_number(totals[-1] if totals else 0),
            "labels": ["24h", "18h", "12h", "6h", "now"],
        }

    def _rank_token_items(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        ranked: list[dict[str, Any]] = []
        for item in items:
            try:
                count = int(item.get("count", 0) or 0)
            except Exception:
                count = 0
            ranked.append({"name": self._safe(item.get("name", "unknown")), "count": count})
        ranked.sort(key=lambda item: item["count"], reverse=True)
        max_count = max((item["count"] for item in ranked), default=0) or 1
        for item in ranked:
            item["percent"] = max(4, min(100, round(item["count"] / max_count * 100))) if item["count"] else 0
            item["value"] = self._fmt_count(item["count"]) if item["count"] else "-"
        return ranked

    def _runtime_cards(self) -> list[dict[str, str]]:
        proc = psutil.Process(os.getpid())
        mem = proc.memory_info()
        return [
            {"label": "HEAP USED", "value": self._fmt_bytes(mem.rss)},
            {"label": "RSS", "value": self._fmt_bytes(mem.rss)},
            {"label": "THREADS", "value": str(proc.num_threads())},
            {"label": "LOOP DELAY", "value": "-"},
        ]

    def _ai_stats(self) -> list[dict[str, str]]:
        error_rate = "-"
        if self._llm_requests:
            error_rate = f"{(self._llm_errors / self._llm_requests) * 100:.1f}%"
        cache_rate = "-"
        if self._cache_hits is not None and self._llm_requests:
            cache_rate = f"{(self._cache_hits / self._llm_requests) * 100:.1f}%"
        return [
            {"label": "请求数", "value": f"{self._llm_requests:,}"},
            {"label": "错误率", "value": error_rate},
            {"label": "缓存命中", "value": cache_rate},
            {"label": "输入 TOKEN", "value": self._fmt_count(self._input_tokens) if self._input_tokens else "-"},
            {"label": "输出 TOKEN", "value": self._fmt_count(self._output_tokens) if self._output_tokens else "-"},
            {"label": "总 TOKEN", "value": self._fmt_count(self._input_tokens + self._output_tokens) if (self._input_tokens + self._output_tokens) else "-"},
        ]

    def _tool_rank(self) -> list[dict[str, Any]]:
        top = self._tool_counter.most_common(6)
        if not top:
            return [{"name": "-", "count": 0, "width": 0}]
        max_count = top[0][1] or 1
        return [
            {
                "name": self._safe(name),
                "count": count,
                "width": round((count / max_count) * 100, 1),
            }
            for name, count in top
        ]

    def _background_style(self, avatar: str = "") -> str:
        path = self._config_file_uri("background_file")
        if path:
            return (
                f"--glass-bg-image: url('{self._safe(path)}'); "
                "background: linear-gradient(150deg, rgba(255, 250, 244, .52), rgba(232, 252, 246, .50));"
            )
        palette = self._monet_palette_from_avatar(avatar)
        return (
            "background: "
            f"radial-gradient(circle at 18% 8%, color-mix(in srgb, {palette['wash']} 76%, var(--accent) 24%) 0, transparent 34%), "
            f"radial-gradient(circle at 78% 18%, color-mix(in srgb, {palette['warm-wash']} 72%, var(--warm) 28%) 0, transparent 32%), "
            f"radial-gradient(circle at 50% 92%, color-mix(in srgb, {palette['accent-soft']} 42%, transparent) 0, transparent 34%), "
            f"linear-gradient(160deg, {palette['bg0']} 0%, {palette['bg1']} 52%, {palette['bg2']} 100%);"
        )

    def _theme_style(self, avatar: str = "") -> str:
        palette = self._monet_palette_from_avatar(avatar)
        return " ".join(
            f"--{key}: {value};" for key, value in palette.items()
        )

    def _monet_palette_from_avatar(self, avatar: str) -> dict[str, str]:
        base = self._avatar_dominant_color(avatar) or (66, 158, 151)
        h, light, sat = colorsys.rgb_to_hls(base[0] / 255, base[1] / 255, base[2] / 255)
        sat = min(max(sat * 0.86, 0.24), 0.58)

        def color(hue_shift: float = 0, l: float = 0.5, s_mul: float = 1.0) -> str:
            hue = (h + hue_shift) % 1.0
            rgb = colorsys.hls_to_rgb(hue, max(0, min(l, 1)), max(0, min(sat * s_mul, 1)))
            return "#" + "".join(f"{int(channel * 255):02x}" for channel in rgb)

        def rgba(hex_color: str, alpha: float) -> str:
            red = int(hex_color[1:3], 16)
            green = int(hex_color[3:5], 16)
            blue = int(hex_color[5:7], 16)
            return f"rgba({red}, {green}, {blue}, {alpha:.2f})"

        accent = color(0, 0.42, 1.15)
        accent_soft = color(0.02, 0.62, 0.82)
        companion = color(0.09, 0.56, 0.92)
        warm = color(0.17, 0.64, 0.72)
        bg0 = color(-0.01, 0.94, 0.52)
        bg1 = color(0.03, 0.90, 0.46)
        bg2 = color(0.09, 0.96, 0.40)
        panel = color(0.01, 0.98, 0.30)
        shell = color(0.01, 0.96, 0.34)
        text = color(0, 0.18, 0.82)
        muted = color(0.01, 0.40, 0.54)
        dark_text = color(0, 0.12, 0.70)
        ring_track = color(0.01, 0.82, 0.38)

        return {
            "page-bg": bg0,
            "text": text,
            "muted": muted,
            "subtle": color(0.02, 0.52, 0.42),
            "accent": accent,
            "accent-soft": accent_soft,
            "accent-2": companion,
            "warm": warm,
            "online": color(0.31, 0.48, 0.90),
            "dark-text": dark_text,
            "ring-track": rgba(ring_track, 0.74),
            "panel-bg": rgba(panel, 0.82),
            "shell-bg": rgba(shell, 0.62),
            "hero-bg": f"linear-gradient(120deg, {rgba(color(0.02, 0.91, 0.46), 0.88)}, {rgba(color(0.08, 0.94, 0.38), 0.82)})",
            "border": rgba(accent, 0.18),
            "border-strong": rgba(accent, 0.30),
            "rule": rgba(accent, 0.40),
            "chip-bg": rgba(color(0.02, 0.96, 0.34), 0.74),
            "ring-inner": color(0.01, 0.97, 0.28),
            "bar-bg": rgba(color(0.02, 0.86, 0.38), 0.66),
            "icon-bg": rgba(accent, 0.12),
            "wash": rgba(color(0.00, 0.86, 0.46), 0.78),
            "warm-wash": rgba(warm, 0.20),
            "bg0": bg0,
            "bg1": bg1,
            "bg2": bg2,
        }

    def _avatar_dominant_color(self, avatar: str) -> tuple[int, int, int] | None:
        if not avatar:
            return None
        try:
            if avatar.startswith("file://"):
                image = Image.open(Path(avatar[7:]))
            elif avatar.startswith(("http://", "https://")):
                request = Request(avatar, headers={"User-Agent": "AstrBot-StatusCard/0.1"})
                with urlopen(request, timeout=3) as response:
                    image = Image.open(io.BytesIO(response.read()))
            else:
                path = Path(avatar)
                if not path.exists():
                    return None
                image = Image.open(path)
            with image:
                image = image.convert("RGB").resize((64, 64))
                pixels = []
                for red, green, blue in image.getdata():
                    h, light, sat = colorsys.rgb_to_hls(red / 255, green / 255, blue / 255)
                    if 0.18 < light < 0.88 and sat > 0.10:
                        pixels.append((red, green, blue))
                if not pixels:
                    pixels = list(image.getdata())
                if not pixels:
                    return None
                red = int(sum(pixel[0] for pixel in pixels) / len(pixels))
                green = int(sum(pixel[1] for pixel in pixels) / len(pixels))
                blue = int(sum(pixel[2] for pixel in pixels) / len(pixels))
                return red, green, blue
        except Exception as exc:
            logger.debug(f"[StatusCard] avatar color extraction failed: {exc}")
            return None

    def _crop_rendered_image(self, image_path: str) -> str:
        path = Path(image_path)
        if not path.exists():
            return image_path
        with Image.open(path) as img:
            crop_width = min(1280, img.width)
            if img.width <= crop_width:
                return image_path
            cropped = img.crop((0, 0, crop_width, img.height))
            out_path = path.with_name(f"{path.stem}_status_card.png")
            cropped.save(out_path)
        return str(out_path)

    def _config_file_uri(self, key: str) -> str:
        value = self._cfg(key, [])
        candidate = self._first_file_candidate(value)
        if not candidate:
            return ""
        resolved = self._resolve_uploaded_file_candidate(key, candidate)
        if resolved:
            candidate = resolved
        return self._normalize_file_url(candidate)

    def _first_file_candidate(self, value: Any) -> str:
        if not value:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            for key in ("path", "file", "url", "name"):
                item = value.get(key)
                if item:
                    return str(item)
            return ""
        if isinstance(value, list):
            for item in value:
                candidate = self._first_file_candidate(item)
                if candidate:
                    return candidate
        return ""

    def _resolve_uploaded_file_candidate(self, key: str, candidate: str) -> str:
        if candidate.startswith(("http://", "https://", "file://")):
            return candidate
        path = Path(candidate)
        if path.exists():
            return str(path)
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path
        except Exception:
            return candidate
        try:
            base = Path(get_astrbot_data_path())
            for root in (
                base / "plugins" / PLUGIN_NAME / "files" / key,
                base / "plugin_data" / PLUGIN_NAME / "files" / key,
            ):
                maybe = root / candidate
                if maybe.exists():
                    return str(maybe)
        except Exception:
            return candidate
        return candidate

    def _find_db_helper(self) -> Any:
        candidates = [self.context]
        for attr in ("core_lifecycle", "_core_lifecycle", "lifecycle", "star_context"):
            value = getattr(self.context, attr, None)
            if value is not None:
                candidates.append(value)
        for root in list(candidates):
            for attr in ("_db", "db_helper", "database", "db", "stat_db", "statistics"):
                value = getattr(root, attr, None)
                if self._looks_like_db_helper(value):
                    return value

            getter = getattr(root, "get_db", None)
            if callable(getter):
                try:
                    value = getter()
                except Exception:
                    value = None
                if self._looks_like_db_helper(value):
                    return value
        try:
            from astrbot.core import db_helper
        except Exception:
            db_helper = None
        if self._looks_like_db_helper(db_helper):
            return db_helper
        return None

    def _looks_like_db_helper(self, value: Any) -> bool:
        if value is None:
            return False
        return callable(getattr(value, "get_db", None)) and (
            callable(getattr(value, "get_base_stats", None))
            or callable(getattr(value, "get_total_message_count", None))
            or callable(getattr(value, "insert_provider_stat", None))
        )

    async def _collect_base_stats_official(self, db_helper: Any, offset_sec: int) -> dict[str, Any]:
        process = psutil.Process(os.getpid())
        plugin_count = await self._count_enabled_plugins(db_helper)
        base = {
            "platform": [],
            "message_count": 0,
            "platform_count": self._count_platform_instances() or 0,
            "plugin_count": plugin_count,
            "plugins": [],
            "message_time_series": [],
            "memory": {
                "process": process.memory_info().rss >> 20,
                "system": psutil.virtual_memory().total >> 20,
            },
            "cpu_percent": round(psutil.cpu_percent(interval=0.5), 1),
            "thread_count": process.num_threads(),
        }
        if db_helper is None:
            return base

        try:
            stat = await self._maybe_await(self._call_any(db_helper, "get_base_stats", offset_sec))
            grouped = await self._maybe_await(self._call_any(db_helper, "get_grouped_base_stats", offset_sec))
            total = await self._maybe_await(self._call_any(db_helper, "get_total_message_count"))
            platform_series = list(getattr(stat, "platform", []) or [])
            now = int(time.time())
            start_time = now - offset_sec
            message_time_series = []
            idx = 0
            for bucket_end in range(start_time, now, 3600):
                cnt = 0
                while idx < len(platform_series) and getattr(platform_series[idx], "timestamp", 0) < bucket_end:
                    cnt += int(getattr(platform_series[idx], "count", 0) or 0)
                    idx += 1
                message_time_series.append([bucket_end, cnt])

            base.update(
                {
                    "platform": [
                        {
                            "name": getattr(item, "name", "unknown"),
                            "count": int(getattr(item, "count", 0) or 0),
                            "timestamp": int(getattr(item, "timestamp", start_time) or start_time),
                        }
                        for item in list(getattr(grouped, "platform", []) or [])
                    ],
                    "message_count": int(total or 0),
                    "message_time_series": message_time_series,
                }
            )
        except Exception as exc:
            logger.debug(f"[StatusCard] official base stats unavailable: {exc}")
        return base

    async def _collect_provider_stats_official(self, db_helper: Any, days: int) -> dict[str, Any]:
        empty = {
            "days": days,
            "trend": {"series": [], "total_series": []},
            "range_total_tokens": 0,
            "range_total_calls": 0,
            "range_avg_ttft_ms": 0,
            "range_avg_duration_ms": 0,
            "range_avg_tpm": 0,
            "range_success_rate": 0,
            "range_by_provider": [],
            "range_by_umo": [],
            "today_total_tokens": 0,
            "today_total_calls": 0,
            "today_by_model": [],
            "today_by_provider": [],
        }
        if db_helper is None:
            logger.warning("[StatusCard] AstrBot database helper not found; provider stats unavailable")
            return empty

        try:
            from sqlmodel import col, select
            from astrbot.core.db.po import ProviderStat
        except Exception as exc:
            logger.warning(f"[StatusCard] ProviderStat import failed: {exc}")
            return empty

        try:
            local_tz = datetime.now().astimezone().tzinfo or timezone.utc
            now_local = datetime.now(local_tz)
            range_start_local = (now_local - timedelta(days=days)).replace(minute=0, second=0, microsecond=0)
            today_start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
            query_start_utc = min(range_start_local, today_start_local).astimezone(timezone.utc)

            async with db_helper.get_db() as session:
                result = await session.execute(
                    select(ProviderStat)
                    .where(
                        ProviderStat.agent_type == "internal",
                        ProviderStat.created_at >= query_start_utc,
                    )
                    .order_by(col(ProviderStat.created_at).asc())
                )
                records = result.scalars().all()

            bucket_timestamps = []
            bucket_cursor = range_start_local
            while bucket_cursor <= now_local:
                bucket_timestamps.append(int(bucket_cursor.timestamp() * 1000))
                bucket_cursor += timedelta(hours=1)

            trend_by_provider: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
            trend_by_model: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
            total_by_provider: dict[str, int] = defaultdict(int)
            total_by_model: dict[str, int] = defaultdict(int)
            total_by_umo: dict[str, int] = defaultdict(int)
            total_by_bucket: dict[int, int] = defaultdict(int)
            today_by_model: dict[str, int] = defaultdict(int)
            today_by_provider: dict[str, int] = defaultdict(int)
            range_total_tokens = 0
            range_total_output_tokens = 0
            range_total_calls = 0
            range_success_calls = 0
            range_ttft_total_ms = 0.0
            range_ttft_samples = 0
            range_duration_total_ms = 0.0
            range_duration_samples = 0
            today_total_tokens = 0
            today_total_calls = 0

            for record in records:
                created_at_utc = self._ensure_aware_utc(getattr(record, "created_at"))
                created_at_local = created_at_utc.astimezone(local_tz)
                token_total = (
                    int(getattr(record, "token_input_other", 0) or 0)
                    + int(getattr(record, "token_input_cached", 0) or 0)
                    + int(getattr(record, "token_output", 0) or 0)
                )
                provider_id = getattr(record, "provider_id", None) or "unknown"
                provider_model = getattr(record, "provider_model", None) or "Unknown"

                if created_at_local >= range_start_local:
                    bucket_local = created_at_local.replace(minute=0, second=0, microsecond=0)
                    bucket_ts = int(bucket_local.timestamp() * 1000)
                    trend_by_provider[provider_id][bucket_ts] += token_total
                    trend_by_model[provider_model][bucket_ts] += token_total
                    total_by_provider[provider_id] += token_total
                    total_by_model[provider_model] += token_total
                    total_by_umo[getattr(record, "umo", None) or "unknown"] += token_total
                    total_by_bucket[bucket_ts] += token_total
                    range_total_tokens += token_total
                    range_total_calls += 1
                    if getattr(record, "status", None) != "error":
                        range_success_calls += 1
                    ttft = float(getattr(record, "time_to_first_token", 0) or 0)
                    if ttft > 0:
                        range_ttft_total_ms += ttft * 1000
                        range_ttft_samples += 1
                    start_time = float(getattr(record, "start_time", 0) or 0)
                    end_time = float(getattr(record, "end_time", 0) or 0)
                    if end_time > start_time:
                        range_duration_total_ms += (end_time - start_time) * 1000
                        range_duration_samples += 1
                        range_total_output_tokens += int(getattr(record, "token_output", 0) or 0)

                if created_at_local >= today_start_local:
                    today_total_calls += 1
                    today_total_tokens += token_total
                    today_by_model[provider_model] += token_total
                    today_by_provider[provider_id] += token_total

            sorted_provider_ids = sorted(total_by_provider.keys(), key=lambda item: total_by_provider[item], reverse=True)
            sorted_model_names = sorted(total_by_model.keys(), key=lambda item: total_by_model[item], reverse=True)
            return {
                "days": days,
                "trend": {
                    "series": [
                        {
                            "name": provider_id,
                            "data": [[bucket_ts, trend_by_provider[provider_id].get(bucket_ts, 0)] for bucket_ts in bucket_timestamps],
                            "total_tokens": total_by_provider[provider_id],
                        }
                        for provider_id in sorted_provider_ids
                    ],
                    "model_series": [
                        {
                            "name": model_name,
                            "data": [[bucket_ts, trend_by_model[model_name].get(bucket_ts, 0)] for bucket_ts in bucket_timestamps],
                            "total_tokens": total_by_model[model_name],
                        }
                        for model_name in sorted_model_names
                    ],
                    "total_series": [[bucket_ts, total_by_bucket.get(bucket_ts, 0)] for bucket_ts in bucket_timestamps],
                },
                "range_total_tokens": range_total_tokens,
                "range_total_calls": range_total_calls,
                "range_avg_ttft_ms": range_ttft_total_ms / range_ttft_samples if range_ttft_samples else 0,
                "range_avg_duration_ms": range_duration_total_ms / range_duration_samples if range_duration_samples else 0,
                "range_avg_tpm": range_total_output_tokens / (range_duration_total_ms / 1000 / 60) if range_duration_total_ms > 0 else 0,
                "range_success_rate": range_success_calls / range_total_calls if range_total_calls else 0,
                "range_by_provider": [
                    {"provider_id": provider_id, "tokens": tokens}
                    for provider_id, tokens in sorted(total_by_provider.items(), key=lambda item: item[1], reverse=True)
                ],
                "range_by_umo": [
                    {"umo": umo, "tokens": tokens}
                    for umo, tokens in sorted(total_by_umo.items(), key=lambda item: item[1], reverse=True)
                ],
                "today_total_tokens": today_total_tokens,
                "today_total_calls": today_total_calls,
                "today_by_model": [
                    {"provider_model": model_name, "tokens": tokens}
                    for model_name, tokens in sorted(today_by_model.items(), key=lambda item: item[1], reverse=True)
                ],
                "today_by_provider": [
                    {"provider_id": provider_id, "tokens": tokens}
                    for provider_id, tokens in sorted(today_by_provider.items(), key=lambda item: item[1], reverse=True)
                ],
            }
        except Exception as exc:
            logger.warning(f"[StatusCard] official provider stats unavailable: {exc}", exc_info=True)
            return empty

    async def _collect_provider_stats_from_db(self, db_helper: Any) -> dict[str, Any]:
        direct = await self._maybe_await(self._call_any(db_helper, "get_provider_stats", 86400))
        if isinstance(direct, dict):
            return direct

        try:
            from sqlmodel import col, select
            from astrbot.core.db.po import ProviderStat
        except Exception:
            return {}

        try:
            db = await self._maybe_await(self._call_any(db_helper, "get_db"))
            if db is None:
                return {}
            start = datetime.now(timezone.utc) - timedelta(days=1)
            async with db.get_db() as session:
                stmt = select(ProviderStat).where(col(ProviderStat.created_at) >= start)
                result = await session.exec(stmt)
                rows = result.all()

            per_provider: Counter[str] = Counter()
            total_tokens = 0
            total_calls = 0
            total_success = 0
            for row in rows:
                provider_id = str(getattr(row, "provider_id", "") or "-")
                tokens = (
                    int(getattr(row, "token_input_other", 0) or 0)
                    + int(getattr(row, "token_input_cached", 0) or 0)
                    + int(getattr(row, "token_output", 0) or 0)
                )
                per_provider[provider_id] += tokens
                total_tokens += tokens
                total_calls += 1
                if str(getattr(row, "status", "") or "").lower() in {"completed", "success", "ok"}:
                    total_success += 1
            rank = [
                {"name": self._safe(name), "count": count}
                for name, count in per_provider.most_common(5)
            ]
            return {
                "rank": rank,
                "total_tokens": total_tokens,
                "call_count": total_calls,
                "success_rate": (total_success / total_calls * 100) if total_calls else None,
            }
        except Exception as exc:
            logger.debug(f"[StatusCard] dashboard provider stats unavailable: {exc}")
            return {}

    def _count_platform_instances(self) -> int | None:
        manager = getattr(self.context, "platform_manager", None)
        if manager is None:
            return None
        for attr in ("platform_insts", "platforms", "instances"):
            value = getattr(manager, attr, None)
            if isinstance(value, dict):
                return len(value)
            if isinstance(value, list):
                return len(value)
        getter = getattr(manager, "get_insts", None)
        if callable(getter):
            try:
                insts = getter()
                return len(insts) if insts is not None else None
            except Exception:
                return None
        return None

    def _rank_from_grouped(self, grouped: dict[str, Any], key: str) -> list[dict[str, Any]]:
        if not isinstance(grouped, dict):
            return []
        raw = grouped.get(key) or grouped.get(f"{key}_rank") or grouped.get("platform")
        if isinstance(raw, dict):
            items = raw.items()
        elif isinstance(raw, list):
            items = []
            for item in raw:
                if isinstance(item, dict):
                    name = item.get("name") or item.get("platform") or item.get("key") or item.get("id")
                    count = item.get("count") or item.get("message_count") or item.get("value")
                    items.append((name, count))
        else:
            return []
        ranked = []
        for name, count in items:
            if name is None:
                continue
            try:
                count = int(count or 0)
            except Exception:
                count = 0
            ranked.append({"name": self._safe(name), "count": count})
        ranked.sort(key=lambda item: item["count"], reverse=True)
        return ranked[:5]

    def _pick_number(self, data: dict[str, Any], *keys: str) -> int | float | None:
        if not isinstance(data, dict):
            return None
        for key in keys:
            value = data.get(key)
            if isinstance(value, (int, float)):
                return value
        return None

    def _display_number(self, value: Any) -> str:
        if value is None:
            return "-"
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if isinstance(value, int):
            return f"{value:,}"
        return str(value)

    def _fmt_mb(self, value: Any) -> str:
        if not isinstance(value, (int, float)):
            return "-"
        if value >= 1024:
            return f"{value / 1024:.1f} GB"
        return f"{int(value):,} MB"

    def _count_plugins(self) -> int:
        for root in (self.context, getattr(self.context, "star_context", None), getattr(self.context, "core_lifecycle", None)):
            if root is None:
                continue
            star_context = getattr(root, "star_context", root)
            getter = getattr(star_context, "get_all_stars", None)
            if callable(getter):
                try:
                    return len(getter() or [])
                except Exception:
                    continue
        return 0

    async def _count_enabled_plugins(self, db_helper: Any) -> int:
        stars = self._get_all_star_metadata()
        if db_helper is not None:
            disabled = await self._get_inactivated_plugins_from_db(db_helper)
            if disabled is not None:
                return sum(1 for star in stars if self._star_identifier(star) not in disabled)
        if stars:
            return sum(1 for star in stars if bool(getattr(star, "activated", True)))
        return self._count_plugins()

    def _get_all_star_metadata(self) -> list[Any]:
        for root in (self.context, getattr(self.context, "star_context", None), getattr(self.context, "core_lifecycle", None)):
            if root is None:
                continue
            star_context = getattr(root, "star_context", root)
            getter = getattr(star_context, "get_all_stars", None)
            if callable(getter):
                try:
                    return list(getter() or [])
                except Exception:
                    continue
        return []

    async def _get_inactivated_plugins_from_db(self, db_helper: Any) -> set[str] | None:
        try:
            from sqlmodel import select
            from astrbot.core.db.po import Preference
        except Exception as exc:
            logger.debug(f"[StatusCard] Preference import failed: {exc}")
            return None

        try:
            async with db_helper.get_db() as session:
                result = await session.execute(
                    select(Preference).where(
                        Preference.scope == "global",
                        Preference.scope_id == "global",
                        Preference.key == "inactivated_plugins",
                    )
                )
                scalars = result.scalars()
                pref = scalars.first() if hasattr(scalars, "first") else None
            value = getattr(pref, "value", None) if pref is not None else None
            if isinstance(value, dict):
                raw = value.get("val", [])
            else:
                raw = []
            return {str(item) for item in raw if item}
        except Exception as exc:
            logger.debug(f"[StatusCard] enabled plugin count from db failed: {exc}")
            return None

    def _star_identifier(self, star: Any) -> str:
        return str(
            getattr(star, "module_path", None)
            or getattr(star, "root_dir_name", None)
            or getattr(star, "name", None)
            or ""
        )

    def _rank_platform_stats(self, stats: Any) -> list[dict[str, Any]]:
        if not isinstance(stats, list):
            return []
        ranked = []
        for item in stats:
            if isinstance(item, dict):
                name = item.get("name") or item.get("platform_id") or "unknown"
                count = item.get("count") or 0
            else:
                name = getattr(item, "name", None) or getattr(item, "platform_id", None) or "unknown"
                count = getattr(item, "count", 0) or 0
            try:
                count = int(count)
            except Exception:
                count = 0
            ranked.append({"name": self._safe(name), "count": count})
        ranked.sort(key=lambda item: item["count"], reverse=True)
        max_count = max((item["count"] for item in ranked), default=0) or 1
        for item in ranked:
            item["percent"] = max(4, min(100, round(item["count"] / max_count * 100))) if item["count"] else 0
        return ranked[:6]

    @staticmethod
    def _ensure_aware_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    async def _maybe_await(self, value: Any) -> Any:
        if asyncio.iscoroutine(value) or hasattr(value, "__await__"):
            return await value
        return value

    def _call_any(self, obj: Any, name: str, *args) -> Any:
        if obj is None:
            return None
        func = getattr(obj, name, None)
        if not callable(func):
            return None
        try:
            return func(*args)
        except TypeError:
            try:
                return func()
            except Exception:
                return None
        except Exception:
            return None

    def _template(self) -> str:
        return r"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<style>
* { box-sizing: border-box; }
:root {
  --page-bg: #e3f5ef;
  --text: #193536;
  --muted: #536f70;
  --subtle: #708887;
  --accent: #3b847d;
  --accent-soft: #8fcac3;
  --accent-2: #6da1b5;
  --warm: #c9a56f;
  --online: #4a9b74;
  --dark-text: #193536;
  --ring-track: rgba(178, 216, 211, .74);
  --panel-bg: linear-gradient(145deg, rgba(255, 255, 255, .62), rgba(255, 255, 255, .30));
  --shell-bg: linear-gradient(150deg, rgba(255, 255, 255, .38), rgba(255, 255, 255, .16));
  --hero-bg: linear-gradient(128deg, rgba(255, 255, 255, .52), rgba(255, 255, 255, .24));
  --border: rgba(255, 255, 255, .58);
  --border-strong: rgba(255, 255, 255, .72);
  --rule: rgba(59, 132, 125, .40);
  --chip-bg: linear-gradient(145deg, rgba(255, 255, 255, .52), rgba(255, 255, 255, .22));
  --ring-inner: rgba(255, 255, 255, .70);
  --bar-bg: rgba(195, 221, 216, .66);
  --icon-bg: rgba(59, 132, 125, .12);
  --glass-bg-image: none;
  --glass-blur: 22px;
}
html {
  margin: 0;
  width: 1280px;
  background: var(--page-bg);
}
body {
  margin: 0;
  width: 1280px;
  min-height: 100vh;
  background: var(--page-bg);
  color: var(--text);
  font-family: "Microsoft YaHei", "Segoe UI", Arial, sans-serif;
  letter-spacing: 0;
}
.page {
  position: relative;
  overflow: hidden;
  width: 1280px;
  padding: 18px;
  background-size: cover;
  background-position: center;
  filter: saturate(1.18);
}
.page::before {
  content: "";
  position: absolute;
  inset: -34px;
  z-index: 0;
  background-image:
    linear-gradient(135deg, rgba(255,255,255,.24), rgba(255,255,255,.04)),
    var(--glass-bg-image);
  background-size: cover;
  background-position: center;
  filter: blur(24px) saturate(1.45) contrast(1.08);
  transform: scale(1.05);
  opacity: .90;
}
.page::after {
  content: "";
  position: absolute;
  inset: 0;
  z-index: 0;
  background:
    radial-gradient(circle at 14% 4%, color-mix(in srgb, var(--accent-soft) 40%, transparent) 0, transparent 26%),
    radial-gradient(circle at 82% 12%, color-mix(in srgb, var(--warm) 36%, transparent) 0, transparent 24%),
    linear-gradient(145deg, rgba(255,255,255,.30), rgba(255,255,255,.08));
  backdrop-filter: blur(6px) saturate(1.25);
}
.shell {
  position: relative;
  z-index: 1;
  padding: 14px;
  border: 1px solid rgba(255, 255, 255, .52);
  border-radius: 28px;
  background: var(--shell-bg);
  backdrop-filter: blur(28px) saturate(1.55);
  -webkit-backdrop-filter: blur(28px) saturate(1.55);
  box-shadow:
    0 28px 64px rgba(26, 54, 56, .20),
    inset 0 1px 0 rgba(255, 255, 255, .78),
    inset 0 -24px 52px rgba(255, 255, 255, .14);
}
.top-grid {
  display: grid;
  grid-template-columns: 430px 1fr;
  gap: 14px;
  align-items: stretch;
}
.panel {
  position: relative;
  overflow: hidden;
  border: 1px solid rgba(255, 255, 255, .62);
  border-radius: 16px;
  background: var(--panel-bg);
  backdrop-filter: blur(var(--glass-blur)) saturate(1.65);
  -webkit-backdrop-filter: blur(var(--glass-blur)) saturate(1.65);
  box-shadow:
    0 12px 28px rgba(24, 56, 57, .14),
    inset 0 1px 0 rgba(255, 255, 255, .82),
    inset 0 -18px 42px rgba(255, 255, 255, .12);
}
.panel::before {
  content: "";
  position: absolute;
  inset: 0;
  z-index: 0;
  pointer-events: none;
  border-radius: inherit;
  background:
    linear-gradient(145deg, rgba(255,255,255,.50), rgba(255,255,255,.10) 42%, rgba(255,255,255,.04)),
    radial-gradient(circle at 16% 0%, rgba(255,255,255,.62), transparent 32%);
  mix-blend-mode: screen;
}
.panel > * {
  position: relative;
  z-index: 1;
}
.hero {
  padding: 20px 18px 22px;
  min-height: 214px;
  background: var(--hero-bg);
}
.title {
  color: var(--accent);
  font-size: 20px;
  font-weight: 800;
  letter-spacing: 5px;
  margin: 0 0 12px;
}
.rule { height: 1px; background: var(--rule); margin-bottom: 14px; }
.bot-name { font-size: 20px; font-weight: 800; margin-bottom: 10px; }
.hero-row { display: flex; gap: 16px; align-items: center; }
.avatar-wrap { position: relative; width: 88px; height: 88px; flex: 0 0 auto; }
.avatar {
  width: 88px; height: 88px; border-radius: 50%;
  border: 4px solid rgba(255, 255, 255, .72);
  object-fit: cover;
  background: color-mix(in srgb, var(--accent) 16%, transparent);
  box-shadow: 0 10px 24px rgba(24, 56, 57, .20), inset 0 1px 0 rgba(255,255,255,.70);
}
.avatar-fallback {
  width: 88px; height: 88px; border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  color: var(--dark-text); background: var(--accent); font-size: 36px; font-weight: 900;
  border: 4px solid color-mix(in srgb, var(--text) 90%, transparent);
}
.dot { position: absolute; right: 2px; bottom: 8px; width: 18px; height: 18px; border-radius: 50%; background: var(--online); border: 2px solid rgba(255,255,255,.78); box-shadow: 0 3px 10px rgba(74,155,116,.42); }
.chips { display: flex; flex-wrap: wrap; gap: 8px; align-content: center; }
.chip {
  border: 1px solid rgba(255,255,255,.66);
  border-radius: 999px;
  padding: 6px 10px;
  color: var(--muted);
  background: var(--chip-bg);
  backdrop-filter: blur(16px) saturate(1.5);
  -webkit-backdrop-filter: blur(16px) saturate(1.5);
  box-shadow: inset 0 1px 0 rgba(255,255,255,.72), 0 4px 12px rgba(24,56,57,.08);
  font-weight: 800;
  font-size: 12px;
  white-space: nowrap;
}
.chip.ok { color: var(--online); }
.section-title {
  color: var(--accent);
  font-size: 14px;
  font-weight: 900;
  margin: 20px 4px 12px;
  letter-spacing: 1px;
}
.metrics { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }
.dashboard-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; }
.content-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-top: 14px; align-items: stretch; }
.content-grid > div { display: flex; flex-direction: column; }
.metric {
  min-height: 214px;
  padding: 20px 12px 14px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 10px;
}
.ring {
  width: 92px; height: 92px; border-radius: 50%;
  background: conic-gradient(var(--accent) calc(var(--p) * 1%), var(--ring-track) 0);
  display: flex; align-items: center; justify-content: center;
  box-shadow: inset 0 1px 0 rgba(255,255,255,.60), 0 10px 24px rgba(24,56,57,.10);
}
.ring::before {
  content: attr(data-value);
  width: 68px; height: 68px; border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  background: var(--ring-inner);
  font-size: 22px;
  font-weight: 900;
}
.metric-name { font-size: 18px; font-weight: 900; }
.metric-text { color: var(--muted); font-size: 11px; text-align: center; line-height: 1.5; max-width: 180px; overflow-wrap: anywhere; }
.chart { padding: 16px; }
.chart svg { width: 100%; height: 128px; display: block; }
.axis { display: flex; justify-content: space-between; color: var(--subtle); font-size: 10px; margin-top: 4px; }
.net-cards, .ai-grid, .runtime-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; }
.ai-grid { grid-template-columns: repeat(3, 1fr); }
.runtime-grid { grid-template-columns: repeat(4, 1fr); }
.mini {
  padding: 18px 16px;
  min-height: 96px;
  display: flex;
  flex-direction: column;
  justify-content: center;
}
.mini.panel {
  border-color: rgba(255,255,255,.58);
}
.label { color: var(--accent); font-size: 11px; font-weight: 900; margin-bottom: 8px; }
.value { font-size: 16px; font-weight: 900; overflow-wrap: anywhere; }
.dashboard-grid .label { font-size: 12px; margin-bottom: 10px; }
.dashboard-grid .value { font-size: 24px; line-height: 1.08; }
.dashboard-grid .metric-text { font-size: 12px; line-height: 1.35; max-width: none; }
.disk { padding: 16px; }
.disk-row { margin-bottom: 14px; }
.disk-head { display: flex; justify-content: space-between; font-size: 13px; font-weight: 800; margin-bottom: 8px; }
.bar { height: 6px; border-radius: 999px; background: rgba(255,255,255,.32); overflow: hidden; box-shadow: inset 0 1px 2px rgba(25,53,54,.10); }
.fill { height: 100%; border-radius: 999px; background: var(--accent); width: var(--w); }
.fill.warn { background: #ffbd24; }
.tool-list { padding: 16px; }
.tool-row { display: grid; grid-template-columns: 132px 1fr 36px; align-items: center; gap: 8px; margin: 10px 0; }
.tool-name { font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.tool-bar { height: 7px; background: var(--bar-bg); border-radius: 999px; overflow: hidden; }
.tool-fill { height: 100%; width: var(--w); border-radius: 999px; background: linear-gradient(90deg, var(--accent-2), var(--accent)); }
.tool-count { color: var(--muted); font-size: 12px; text-align: right; }
.message-panel { padding: 16px; min-height: 252px; flex: 1; }
.message-total {
  display: grid;
  grid-template-columns: 1fr auto;
  align-items: end;
  gap: 12px;
  padding-bottom: 14px;
  margin-bottom: 6px;
  border-bottom: 1px solid color-mix(in srgb, var(--accent) 12%, transparent);
}
.message-total .label { margin-bottom: 6px; }
.message-total-value { font-size: 30px; font-weight: 900; line-height: 1; }
.message-total-sub { color: var(--muted); font-size: 11px; font-weight: 800; text-align: right; }
.message-chart-meta { display: flex; justify-content: space-between; color: var(--muted); font-size: 11px; font-weight: 800; margin-bottom: 8px; }
.message-chart svg { width: 100%; height: 138px; display: block; }
.message-grid-line { stroke: color-mix(in srgb, var(--accent) 16%, transparent); stroke-width: 1; }
.message-area { fill: color-mix(in srgb, var(--accent-2) 22%, transparent); }
.message-line { fill: none; stroke: var(--accent); stroke-width: 4; stroke-linecap: round; stroke-linejoin: round; }
.message-dot { fill: var(--accent); stroke: var(--ring-inner); stroke-width: 3; }
.message-axis { display: flex; justify-content: space-between; color: var(--subtle); font-size: 10px; margin-top: 4px; }
.session-panel { padding: 14px 16px; min-height: 252px; flex: 1; overflow: hidden; }
.session-row { display: grid; grid-template-columns: 30px 1fr 72px; align-items: center; gap: 10px; padding: 5px 0; min-height: 24px; }
.session-index { color: var(--accent); font-size: 13px; font-weight: 900; }
.session-name { font-size: 13.5px; font-weight: 900; line-height: 1.15; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.session-bar { height: 7px; background: rgba(255,255,255,.34); border-radius: 999px; overflow: hidden; margin-top: 4px; box-shadow: inset 0 1px 2px rgba(25,53,54,.10); }
.session-fill { height: 100%; width: var(--w); border-radius: 999px; background: linear-gradient(90deg, var(--accent-2), var(--accent)); }
.session-value { color: var(--muted); font-size: 13.5px; font-weight: 900; text-align: right; }
.model-panel { padding: 16px; }
.model-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; align-items: stretch; }
.model-chart-card { min-height: 220px; }
.model-chart-head { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; margin-bottom: 12px; }
.model-kpi { border: 1px solid var(--border); border-radius: 12px; padding: 10px; background: color-mix(in srgb, var(--panel-bg) 78%, transparent); }
.model-kpi .label { margin-bottom: 6px; }
.model-chart-meta { display: flex; justify-content: space-between; color: var(--muted); font-size: 11px; font-weight: 800; margin-bottom: 8px; }
.model-chart svg { width: 100%; height: 142px; display: block; }
.model-bar-segment { shape-rendering: geometricPrecision; }
.model-legend { display: flex; flex-wrap: wrap; gap: 8px 12px; margin-top: 8px; }
.model-legend-item { display: inline-flex; align-items: center; gap: 6px; color: var(--muted); font-size: 11px; font-weight: 900; max-width: 180px; }
.model-swatch { width: 10px; height: 10px; border-radius: 3px; flex: 0 0 auto; }
.model-legend-name { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.model-lists { display: grid; grid-template-columns: 1fr; gap: 12px; }
.model-list { padding: 14px 16px; min-height: 220px; overflow: hidden; }
.model-list-title { color: var(--accent); font-size: 15px; font-weight: 900; margin-bottom: 12px; }
.model-row { display: grid; grid-template-columns: 1fr 78px; align-items: center; gap: 12px; padding: 8px 0; min-height: 33px; border-bottom: 1px solid color-mix(in srgb, var(--accent) 10%, transparent); }
.model-row:last-child { border-bottom: 0; }
.model-name { font-size: 14.5px; font-weight: 900; line-height: 1.2; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.model-value { color: var(--muted); font-size: 14px; font-weight: 900; text-align: right; }
.rank-row { display: grid; grid-template-columns: 1fr 92px; align-items: center; gap: 12px; padding: 12px 0; border-bottom: 1px solid color-mix(in srgb, var(--accent) 12%, transparent); }
.rank-row:last-child { border-bottom: 0; }
.rank-name { font-size: 13px; font-weight: 800; overflow-wrap: anywhere; }
.rank-count { font-size: 14px; font-weight: 900; text-align: right; }
.sys { padding: 16px; }
.sys-row { display: grid; grid-template-columns: 72px 1fr; gap: 10px; margin: 9px 0; font-size: 13px; }
.sys-key { color: var(--accent); font-weight: 900; }
.sys-val { font-weight: 800; overflow-wrap: anywhere; }
</style>
</head>
<body>
<div class="page" style="{{ theme_style }} {{ background_style }}">
  <div class="shell">
    <div class="top-grid">
      <section class="hero panel">
        <div class="title">{{ title }}</div>
        <div class="rule"></div>
        <div class="bot-name">{{ bot.name }}</div>
        <div class="hero-row">
          <div class="avatar-wrap">
            {% if bot.avatar %}<img class="avatar" src="{{ bot.avatar }}">{% else %}<div class="avatar-fallback">A</div>{% endif %}
            <div class="dot"></div>
          </div>
          <div class="chips">
            <div class="chip">{{ bot.id }}</div>
            <div class="chip ok">在线</div>
            <div class="chip">好友 {{ platform.friends }}</div>
            <div class="chip">群聊 {{ platform.groups }}</div>
            <div class="chip">模型 {{ session.model }}</div>
            <div class="chip">人格 {{ session.persona }}</div>
            <div class="chip">{{ bot.status }}</div>
          </div>
        </div>
      </section>

      <div>
        <div class="section-title" style="margin-top: 2px;">系统性能</div>
        <div class="metrics">
          {% for item in [system.cpu, system.memory, system.process_memory] %}
          <div class="metric panel">
            <div class="ring" style="--p: {{ item.percent if item.percent is not none else 0 }}" data-value="{{ (item.percent|string + '%') if item.percent is not none else '-' }}"></div>
            <div class="metric-name">{{ ["CPU", "内存", "进程内存"][loop.index0] }}</div>
            <div class="metric-text">{{ item.text }}<br>{{ item.sub }}</div>
          </div>
          {% endfor %}
        </div>
      </div>
    </div>

    <div class="section-title">WebUI 统计概览</div>
    <div class="dashboard-grid">
      {% for item in dashboard.cards %}
      <div class="mini panel"><div class="label">{{ item.label }}</div><div class="value">{{ item.value }}</div><div class="metric-text" style="text-align:left;margin-top:8px;">{{ item.sub }}</div></div>
      {% endfor %}
    </div>

    <div class="content-grid">
      {% if show_message_stats %}
      <div>
        <div class="section-title">消息概览</div>
        <div class="message-panel panel">
          <div class="message-total">
            <div>
              <div class="label">消息总数</div>
              <div class="message-total-value">{{ dashboard.message_total }}</div>
            </div>
            <div class="message-total-sub">平台消息累计</div>
          </div>
          <div class="message-chart-meta">
            <span>最近 24 小时消息曲线</span>
            <span>峰值 {{ dashboard.message_chart.max }} · 当前 {{ dashboard.message_chart.latest }}</span>
          </div>
          <div class="message-chart">
            <svg viewBox="0 0 520 138" preserveAspectRatio="none">
              <line class="message-grid-line" x1="0" y1="32" x2="520" y2="32"></line>
              <line class="message-grid-line" x1="0" y1="74" x2="520" y2="74"></line>
              <line class="message-grid-line" x1="0" y1="116" x2="520" y2="116"></line>
              {% if dashboard.message_chart.area_path %}<path class="message-area" d="{{ dashboard.message_chart.area_path }}"></path>{% endif %}
              {% if dashboard.message_chart.path %}<path class="message-line" d="{{ dashboard.message_chart.path }}"></path>{% endif %}
            </svg>
          </div>
          <div class="message-axis">
          {% for label in dashboard.message_chart.labels %}
            <span>{{ label }}</span>
          {% endfor %}
          </div>
        </div>
      </div>
      {% endif %}

      {% if show_model_stats %}
      <div>
        <div class="section-title">最近一天会话 token 排名</div>
        <div class="session-panel panel">
          {% if dashboard.session_rank %}
            {% for item in dashboard.session_rank %}
            <div class="session-row">
              <div class="session-index">{{ loop.index }}</div>
              <div>
                <div class="session-name">{{ item.name }}</div>
                <div class="session-bar"><div class="session-fill" style="--w: {{ item.percent }}%"></div></div>
              </div>
              <div class="session-value">{{ item.value }}</div>
            </div>
            {% endfor %}
          {% else %}
            <div class="message-empty">暂无可读取的会话词元统计</div>
          {% endif %}
        </div>
      </div>
      {% endif %}
    </div>

    {% if show_model_stats %}
    <div class="section-title">模型调用</div>
    <div class="model-panel panel">
      <div class="model-grid">
        <div class="model-chart-card">
          <div class="model-chart-head">
            <div class="model-kpi"><div class="label">最近 1 天 TOKEN</div><div class="value">{{ dashboard.model_total_tokens }}</div></div>
            <div class="model-kpi"><div class="label">调用次数</div><div class="value">{{ dashboard.model_calls }}</div></div>
            <div class="model-kpi"><div class="label">调用成功率</div><div class="value">{{ dashboard.model_success_rate }}</div></div>
          </div>
          <div class="model-chart-meta">
            <span>最近 1 天模型 token 柱状图</span>
            <span>峰值 {{ dashboard.model_chart.max }} · 当前 {{ dashboard.model_chart.latest }}</span>
          </div>
          <div class="model-chart">
            <svg viewBox="0 0 820 142" preserveAspectRatio="none">
              <line class="message-grid-line" x1="0" y1="34" x2="820" y2="34"></line>
              <line class="message-grid-line" x1="0" y1="76" x2="820" y2="76"></line>
              <line class="message-grid-line" x1="0" y1="118" x2="820" y2="118"></line>
              {% for bar in dashboard.model_chart.bars %}
                {% for seg in bar.segments %}
                <rect class="model-bar-segment" x="{{ seg.x }}" y="{{ seg.y }}" width="{{ seg.width }}" height="{{ seg.height }}" fill="{{ seg.color }}" rx="2"></rect>
                {% endfor %}
              {% endfor %}
            </svg>
          </div>
          <div class="model-legend">
          {% for item in dashboard.model_chart.legend %}
            <div class="model-legend-item"><span class="model-swatch" style="background: {{ item.color }}"></span><span class="model-legend-name">{{ item.name }}</span></div>
          {% endfor %}
          </div>
          <div class="message-axis">
          {% for label in dashboard.model_chart.labels %}
            <span>{{ label }}</span>
          {% endfor %}
          </div>
        </div>
        <div class="model-lists">
          <div class="model-list panel">
            <div class="model-list-title">模型调用排名</div>
            {% if dashboard.model_daily_rank %}
            {% for item in dashboard.model_daily_rank %}
            <div class="model-row"><div class="model-name">{{ item.name }}</div><div class="model-value">{{ item.value }}</div></div>
            {% endfor %}
            {% else %}
            <div class="message-empty">暂无 Model 统计</div>
            {% endif %}
          </div>
        </div>
      </div>
    </div>
    {% endif %}
  </div>
</div>
</body>
</html>
"""

    def _line_path(self, values: list[float], max_value: float, width: int, height: int) -> str:
        if not values:
            return ""
        if len(values) == 1:
            x_values = [0]
        else:
            x_values = [(idx / (len(values) - 1)) * width for idx in range(len(values))]
        coords = []
        for x, value in zip(x_values, values):
            y = height - (value / max_value) * (height - 12) - 6
            coords.append((round(x, 2), round(max(min(y, height), 0), 2)))
        first = coords[0]
        rest = " ".join(f"L {x} {y}" for x, y in coords[1:])
        return f"M {first[0]} {first[1]} {rest}"

    def _area_path(self, values: list[float], max_value: float, width: int, height: int) -> str:
        line = self._line_path(values, max_value, width, height)
        if not line:
            return ""
        return f"{line} L {width} {height} L 0 {height} Z"

    def _sample_interval_guess(self) -> float:
        if len(self._network_points) < 2:
            return float(self._cfg("network_sample_interval_seconds", 5))
        points = list(self._network_points)[-6:]
        intervals = [points[i].ts - points[i - 1].ts for i in range(1, len(points))]
        return sum(intervals) / len(intervals)

    def _fmt_bytes(self, value: float) -> str:
        try:
            value = float(value)
        except Exception:
            return "-"
        units = ["B", "KB", "MB", "GB", "TB", "PB"]
        idx = 0
        while value >= 1024 and idx < len(units) - 1:
            value /= 1024
            idx += 1
        if idx == 0:
            return f"{value:.0f} {units[idx]}"
        return f"{value:.2f} {units[idx]}"

    def _fmt_rate(self, value: float) -> str:
        return f"{self._fmt_bytes(value)}/s"

    def _fmt_count(self, value: int) -> str:
        if value >= 1_000_000:
            return f"{value / 1_000_000:.2f}M"
        if value >= 1_000:
            return f"{value / 1_000:.1f}K"
        return str(value)

    def _extract_token_count(self, obj: Any, names: tuple[str, ...]) -> int:
        total = 0
        for name in names:
            value = self._deep_get(obj, name)
            if isinstance(value, (int, float)):
                total += int(value)
        usage = self._deep_get(obj, "usage")
        if usage is not None:
            for name in names:
                value = self._deep_get(usage, name)
                if isinstance(value, (int, float)):
                    total += int(value)
        return total

    def _extract_optional_int(self, obj: Any, names: tuple[str, ...]) -> int | None:
        for name in names:
            value = self._deep_get(obj, name)
            if isinstance(value, bool):
                return 1 if value else 0
            if isinstance(value, (int, float)):
                return int(value)
        return None

    def _deep_get(self, obj: Any, key: str) -> Any:
        if obj is None:
            return None
        if isinstance(obj, dict):
            return obj.get(key)
        return getattr(obj, key, None)

    def _looks_like_error(self, response: Any) -> bool:
        for name in ("error", "is_error", "failed"):
            value = self._deep_get(response, name)
            if value:
                return True
        status = self._deep_get(response, "status")
        return isinstance(status, str) and status.lower() in {"error", "failed"}

    def _call_event(self, event: AstrMessageEvent, name: str) -> str:
        try:
            func = getattr(event, name, None)
            if callable(func):
                value = func()
                if value:
                    return str(value)
        except Exception:
            pass
        return ""

    def _avatar_url(self, bot_id: str) -> str:
        if bot_id and bot_id != "-":
            return f"https://q1.qlogo.cn/g?b=qq&nk={bot_id}&s=640"
        return ""

    def _normalize_file_url(self, value: str) -> str:
        if not value:
            return ""
        if value.startswith(("http://", "https://", "file://")):
            return value
        path = Path(value)
        if path.exists():
            return path.resolve().as_uri()
        return value

    def _safe(self, value: Any) -> str:
        return html.escape(str(value), quote=True)
