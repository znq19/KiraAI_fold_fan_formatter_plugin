import re
import asyncio
from core.plugin import BasePlugin, logger, on, Priority
from core.chat.message_utils import KiraMessageBatchEvent
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

        # 转义正则特殊字符
        escaped_start = re.escape(self.tag_start)
        escaped_end = re.escape(self.tag_end)
        self.pattern = re.compile(f'{escaped_start}(.*?){escaped_end}', re.DOTALL)

    async def initialize(self):
        logger.info(f"ThreePartFormatPlugin initialized with tags: {self.tag_start}...{self.tag_end}, auto_length={self.enable_auto_by_length}/{self.auto_length_threshold}")

    async def terminate(self):
        logger.info("ThreePartFormatPlugin terminated")

    @on.after_xml_parse(priority=Priority.HIGH)
    async def on_after_xml_parse(self, event: KiraMessageBatchEvent, message_chains: list):
        if not self.enabled:
            return
        if self.only_group and not event.is_group_message():
            return

        new_chains = []
        for chain in message_chains:
            full_text = "".join(
                elem.text for elem in chain.message_list if isinstance(elem, Text)
            )
            # 检查是否匹配标签
            match = self.pattern.search(full_text)
            should_convert = False
            inner_content = ""
            before = ""
            after = ""

            if match:
                # 有标签，按标签拆分
                before = full_text[:match.start()]
                inner_content = match.group(1).strip()
                after = full_text[match.end():]
                should_convert = True
            elif self.enable_auto_by_length:
                # 无标签但开启自动长度检测
                if len(full_text) > self.auto_length_threshold:
                    before = ""   # 整个消息作为内部内容
                    inner_content = full_text.strip()
                    after = ""
                    should_convert = True
                    logger.info(f"消息长度 {len(full_text)} 超过阈值 {self.auto_length_threshold}，自动转换")

            if not should_convert:
                new_chains.append(chain)
                continue

            # 获取适配器实例及客户端
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
            session_id = event.session.session_id
            self_id = str(event.self_id) if hasattr(event, 'self_id') else "0"
            bot_nick = getattr(adapter_inst.info, 'name', adapter_name)

            # 构造合并转发节点（仅 inner_content）
            nodes = [{
                "type": "node",
                "data": {
                    "name": bot_nick,
                    "uin": self_id,
                    "content": [{"type": "text", "data": {"text": inner_content}}]
                }
            }]

            # 依次发送：引导语 -> 合并转发卡片 -> 后续文本
            try:
                if before.strip():
                    msg = [{"type": "text", "data": {"text": before}}]
                    if session_type == "group":
                        await client.send_action("send_group_msg", {
                            "group_id": int(session_id),
                            "message": msg
                        })
                    else:
                        await client.send_action("send_private_msg", {
                            "user_id": int(session_id),
                            "message": msg
                        })
                    await asyncio.sleep(0.1)  # 微小延迟保证顺序
                # 发送合并转发
                if session_type == "group":
                    await client.send_action("send_forward_msg", {
                        "group_id": int(session_id),
                        "messages": nodes
                    })
                else:
                    await client.send_action("send_forward_msg", {
                        "user_id": int(session_id),
                        "messages": nodes
                    })
                if after.strip():
                    await asyncio.sleep(0.1)
                    msg = [{"type": "text", "data": {"text": after}}]
                    if session_type == "group":
                        await client.send_action("send_group_msg", {
                            "group_id": int(session_id),
                            "message": msg
                        })
                    else:
                        await client.send_action("send_private_msg", {
                            "user_id": int(session_id),
                            "message": msg
                        })
                logger.info(f"已转换发送至 {event.session.sid} (标签模式={match is not None})")
            except Exception as e:
                logger.error(f"转换发送失败: {e}")
                new_chains.append(chain)
                continue

            # 原链已被手动拆分发送，不再加入 new_chains
        message_chains[:] = new_chains