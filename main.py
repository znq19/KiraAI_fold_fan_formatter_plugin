import re
import asyncio
import hashlib
from core.plugin import BasePlugin, logger, on, Priority
from core.chat.message_utils import KiraMessageBatchEvent, KiraIMSentResult
from core.chat.message_elements import Text
from core.chat import MessageChain


class ThreePartFormatPlugin(BasePlugin):
    def __init__(self, ctx, cfg):
        super().__init__(ctx, cfg)
        self.enabled = cfg.get("enabled", True)
        self.only_group = cfg.get("only_group", True)
        self.tag_start = cfg.get("tag_start", "[3p]")
        self.tag_end = cfg.get("tag_end", "[/3p]")
        self.enable_auto_by_length = cfg.get("enable_auto_by_length", False)
        self.auto_length_threshold = int(cfg.get("auto_length_threshold", 100))

        escaped_start = re.escape(self.tag_start)
        escaped_end = re.escape(self.tag_end)
        self.pattern = re.compile(f'{escaped_start}(.*?){escaped_end}', re.DOTALL)

    async def initialize(self):
        logger.info(f"ThreePartFormatPlugin initialized with tags: {self.tag_start}...{self.tag_end}, auto_length={self.enable_auto_by_length}/{self.auto_length_threshold}")

    async def terminate(self):
        logger.info("ThreePartFormatPlugin terminated")

    async def _broadcast_sent(self, event, sent: list):
        """补广播 ON_MESSAGE_SENT：手动发送绕过了框架的发送层，
        不补的话订阅该事件的插件（sustained / memory / token-stats 等）
        会完全看不到这三条消息。

        容错：广播失败只记 debug —— 消息已经真的发出去了，
        通知不到下游插件不构成"发送失败"。
        """
        try:
            from core.plugin.plugin_handlers import event_handler_reg, EventType
            handlers = event_handler_reg.get_handlers(EventType.ON_MESSAGE_SENT)
            if not handlers:
                return
            for chain, result in sent:
                for handler in handlers:
                    await handler.exec_handler(event, chain, result)
                    if getattr(event, "is_stopped", False):
                        return
        except Exception as e:  # noqa: BLE001
            logger.debug(f"补广播 ON_MESSAGE_SENT 失败（不影响已发送的消息）: {e}")

    @on.after_xml_parse(priority=Priority.HIGH)
    async def on_after_xml_parse(self, event: KiraMessageBatchEvent, message_chains: list):
        if not self.enabled:
            return
        # 事件已被停止 ⇒ 不再发送（与核心各阶段的约定一致）
        if getattr(event, "is_stopped", False):
            return
        if self.only_group and not event.is_group_message():
            return

        new_chains = []
        for chain in message_chains:
            # 只处理 MessageChain 对象，RootTagAction 等其它类型保持原样
            if not isinstance(chain, MessageChain):
                new_chains.append(chain)
                continue

            full_text = "".join(
                elem.text for elem in chain.message_list if isinstance(elem, Text)
            )
            match = self.pattern.search(full_text)
            should_convert = False
            inner_content = ""
            before = ""
            after = ""

            if match:
                before = full_text[:match.start()]
                inner_content = match.group(1).strip()
                after = full_text[match.end():]
                should_convert = True
            elif self.enable_auto_by_length:
                if len(full_text) > self.auto_length_threshold:
                    before = ""
                    inner_content = full_text.strip()
                    after = ""
                    should_convert = True
                    logger.info(f"消息长度 {len(full_text)} 超过阈值 {self.auto_length_threshold}，自动转换")

            if not should_convert:
                new_chains.append(chain)
                continue

            # ★★ 幂等（防重复发送的关键）：同一个 event 对象在本轮会被派发**两遍** ——
            #   抢先发送插件（accelerator）流式期间补广播一次，框架最终发送时再派发一次。
            #   第一遍我们已经把内容发出去了，第二遍必须**静默吃掉**（链照样移除，
            #   但绝不再发），否则用户会看到穗/卡片各两份。
            #   指纹记在 event 对象上：随本轮结束自然销毁，不会泄漏到下一轮。
            fp = hashlib.sha1(
                f"{getattr(event, 'sid', '')}|{full_text}".encode("utf-8")
            ).hexdigest()
            seen = event.__dict__.setdefault("_foldfan_done", set())
            if fp in seen:
                logger.info("该段本轮已转换发送过（同一事件的重复派发），已跳过（防重复）")
                continue

            adapter_name = event.adapter.name
            adapter_inst = self.ctx.adapter_mgr.get_adapter(adapter_name)
            if not adapter_inst:
                logger.error(f"无法获取适配器实例 {adapter_name}")
                new_chains.append(chain)
                continue
            client = adapter_inst.get_client()
            if not client:
                logger.error("无法获取QQ客户端")
                new_chains.append(chain)
                continue

            session_type = "group" if event.is_group_message() else "private"
            # ★ 目标 id 从规范 sid（<adapter>:<dm|gm>:<id>）解析，
            #   不再假设 session.session_id 一定是纯数字。
            target = str(getattr(event.session, "session_id", "") or "")
            try:
                parts = str(getattr(event, "sid", "") or "").split(":", 2)
                if len(parts) == 3 and parts[2]:
                    target = parts[2]
            except Exception:  # noqa: BLE001
                pass
            try:
                target = int(target)
            except (TypeError, ValueError):
                pass  # 非纯数字 id：原样传给适配器

            # ★ self_id 缺省时不再硬编码 "0"，尽量从适配器信息里取
            self_id = str(getattr(event, "self_id", "") or "")
            if not self_id:
                info = getattr(adapter_inst, "info", None)
                self_id = str(getattr(info, "self_id", "") or getattr(info, "uin", "") or "0")
            bot_nick = getattr(getattr(adapter_inst, "info", None), 'name', adapter_name)

            nodes = [{
                "type": "node",
                "data": {
                    "name": bot_nick,
                    "uin": self_id,
                    "content": [{"type": "text", "data": {"text": inner_content}}]
                }
            }]

            async def _send_text(text):
                msg = [{"type": "text", "data": {"text": text}}]
                if session_type == "group":
                    return await client.send_action("send_group_msg", {
                        "group_id": target, "message": msg
                    })
                return await client.send_action("send_private_msg", {
                    "user_id": target, "message": msg
                })

            def _result_of(resp):
                mid = None
                try:
                    mid = (resp or {}).get("data", {}).get("message_id")
                except Exception:  # noqa: BLE001
                    mid = None
                return KiraIMSentResult(
                    message_id=str(mid) if mid is not None else None, ok=True)

            sent_steps = []     # 已完成到哪一步（异常时只对未发部分降级）
            sent_msgs = []      # (chain, result)，供补广播 ON_MESSAGE_SENT
            try:
                if before.strip():
                    resp = await _send_text(before)
                    sent_steps.append("before")
                    sent_msgs.append((MessageChain([Text(before)]), _result_of(resp)))
                    await asyncio.sleep(0.1)

                if session_type == "group":
                    resp = await client.send_action("send_forward_msg", {
                        "group_id": target, "messages": nodes
                    })
                else:
                    resp = await client.send_action("send_forward_msg", {
                        "user_id": target, "messages": nodes
                    })
                sent_steps.append("forward")
                sent_msgs.append((MessageChain([Text(inner_content)]), _result_of(resp)))

                if after.strip():
                    await asyncio.sleep(0.1)
                    resp = await _send_text(after)
                    sent_steps.append("after")
                    sent_msgs.append((MessageChain([Text(after)]), _result_of(resp)))

                # 全部成功才记指纹 ⇒ 失败时允许（由降级链或下次派发）重试
                seen.add(fp)
                logger.info(f"已转换发送至 {event.session.sid} (标签模式={match is not None})")
                # ★ 补广播 ON_MESSAGE_SENT（手动发送绕过框架发送层，必须补）
                await self._broadcast_sent(event, sent_msgs)
            except Exception as e:
                logger.error(f"转换发送失败: {e}")
                # ★ 只对「还没发出去」的部分降级为纯文本交回框架，
                #   已发出的部分绝不回退 —— 否则已发部分会再出现一次（部分重复）。
                remaining = []
                if "before" not in sent_steps and before.strip():
                    remaining.append(before.strip())
                if "forward" not in sent_steps:
                    remaining.append(inner_content)
                if "after" not in sent_steps and after.strip():
                    remaining.append(after.strip())
                if remaining:
                    logger.warning(f"已发出 {len(sent_steps)} 部分，仅将未发部分降级为普通消息")
                    new_chains.append(MessageChain([Text("\n".join(remaining))]))
                continue

            # 已通过手动发送处理，原链不加入 new_chains
        message_chains[:] = new_chains
