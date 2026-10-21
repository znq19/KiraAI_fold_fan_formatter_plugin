"""一次性配置迁移（v1.1.0 起）—— 把字符串形态的起始/结束标签迁移为列表。

## 背景

v1.1.0 起 ``tag_start`` / ``tag_end`` 的 schema 类型从 ``string`` 改为 ``list``
（WebUI 一回车一个，可配置多个等价标签）。框架 ``_ensure_plugin_config``
的合并规则是"只给**缺失**的键补默认值"——老用户配置文件里早已存着
``"tag_start": "[3p]"`` 这样的**字符串**，会被原样保留。
插件运行时已做兼容（str 自动归一为单元素列表，功能不受影响），但 WebUI 的
TagInput 拿到字符串会按字符逐个渲染，配置面板显示错乱。

⇒ 升级后做一次**显式迁移**：把字符串值改写为单元素列表，面板即恢复正常。

## 设计（与 accelerator v1.0.78 的迁移同套安全准则）

- **仅一次**：完成标记放在插件数据目录（``plugin_data/<id>/config_migrations.json``）。
  标记存在 ⇒ 以后一律不动用户的设置。
- **走框架自己的通道**：用 ``plugin_mgr.update_plugin_config()`` 写配置
  （与面板"保存并热重载"完全同一条路：更新内存 + 落盘 + 重载插件）。
  **绝不直接改配置文件** —— 那样内存缓存与磁盘会不一致，面板和下次保存都会乱。
- **不重入**：该调用会热重载本插件，所以不能在 ``initialize()`` 的调用栈里立刻执行；
  由 ``main.py`` 延迟到初始化完成后执行一次。
- **只动需要动的键**：值仍是字符串的键才迁移；已是列表/键缺失 ⇒ 只记标记，
  不改配置、不触发重载。
- **失败安静**：任何一步失败（无管理器、无数据目录、写盘失败…）都只记日志跳过，
  绝不影响插件其余功能；下次加载会重试。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

#: 迁移延迟（秒）—— 等插件初始化完成后再执行（它会把插件热重载一次）
MIGRATION_DELAY_S = 3.0

#: 标记文件名（插件数据目录内）
MARKER_FILE = "config_migrations.json"

#: 本次迁移的键名（将来还有别的迁移就往后追加，互不干扰）
KEY = "tags_list_v1_1_0"

#: 参与迁移的配置键
TAG_KEYS = ("tag_start", "tag_end")

#: 各键的默认列表（与 main.py / schema.json 保持一致；
#: 不 import main.py 以避免循环导入 —— main 已 import 本模块）
DEFAULTS = {"tag_start": ["[3p]"], "tag_end": ["[/3p]"]}


def marker_applied(data_dir) -> bool:
    """迁移标记是否已存在且表示"已处理过"。"""
    if data_dir is None:
        return False
    try:
        f = Path(data_dir) / MARKER_FILE
        if not f.is_file():
            return False
        data = json.loads(f.read_text(encoding="utf-8"))
        return bool(isinstance(data, dict) and data.get(KEY))
    except Exception:  # noqa: BLE001
        return False


def write_marker(data_dir, *, migrated: list) -> None:
    """写入迁移标记（先写临时文件再原子替换；保留文件里其它迁移的键）。"""
    if data_dir is None:
        return
    f = Path(data_dir) / MARKER_FILE
    data: dict = {}
    try:
        if f.is_file():
            loaded = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
    except Exception:  # noqa: BLE001
        data = {}
    data[KEY] = {"applied": True, "migrated": list(migrated), "at": int(time.time())}
    tmp = f.with_name(f.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(f)


def decide_plan(cfg) -> list:
    """返回需要迁移的键列表（值仍为**字符串**形态的键）。

    空列表 = 无需改动、只记标记。只有严格是 str 才迁移；
    缺失/已是列表/其它怪值一律不动（怪值由运行时归一兜底）。
    """
    plan = []
    if isinstance(cfg, dict):
        for k in TAG_KEYS:
            if isinstance(cfg.get(k), str):
                plan.append(k)
    return plan


async def run_tags_migration(plugin, plugin_id: str, data_dir, log) -> str:
    """执行迁移。返回 'migrated' / 'marked' / 'skip' / 'error'（也是测试的断言接口）。

    调用前提（由调用方保证）：插件已加载完成、data_dir 为插件数据目录、log 为 logger。
    本函数自带全部守卫，任何情况下不抛异常（失败返回 'error'/'skip'）。
    """
    if marker_applied(data_dir):
        return "skip"
    mgr = getattr(getattr(plugin, "ctx", None), "plugin_mgr", None)
    if mgr is None:
        return "skip"                      # 无管理器（测试环境等）：不迁移
    # 插件还在册（没被卸载/替换）才继续 —— 定时器可能晚于卸载触发
    insts = getattr(mgr, "plugin_instances", None)
    if isinstance(insts, dict) and insts.get(plugin_id) is not plugin:
        return "skip"
    try:
        if not mgr.is_plugin_enabled(plugin_id):
            return "skip"                  # 已被用户停用：不动，等重新启用
    except Exception:  # noqa: BLE001
        pass
    try:
        current = mgr.get_plugin_config(plugin_id)
        plan = decide_plan(current if isinstance(current, dict) else {})
        if plan:
            # ★ 只提交需要迁移的键（其它配置原样保留），且走框架通道。
            # 迁移时顺手 strip；全空白的病理值回退默认（与运行时归一的语义一致）。
            patch = {}
            for k in plan:
                v = current[k].strip()
                patch[k] = [v] if v else list(DEFAULTS[k])
            await mgr.update_plugin_config(plugin_id, patch)
            write_marker(data_dir, migrated=plan)
            log.info("[fold_fan] 配置迁移（仅一次）：%s 已由字符串改写为列表；"
                     "现在可在面板里一回车一个地配置多个等价标签", ", ".join(plan))
            return "migrated"
        write_marker(data_dir, migrated=[])
        return "marked"
    except Exception as e:  # noqa: BLE001
        log.warning("[fold_fan] 配置迁移未完成（不影响其它功能，下次加载会重试）：%s", e)
        return "error"
