"""Human-only CLI configuration of QQ automatic sync; never an Agent tool."""
import json
import os
from pathlib import Path
import re
import tempfile

from .qq_conversations import build_conversation_list, conversation_identity
from .qq_sample_reader import _build_client, print_conversation_list
from .qq_client import QQClientError

WHITELIST_PATH = Path(__file__).resolve().parent.parent / "data/qq/sync_conversations.json"


def load_whitelist(path=None):
    """Fail closed: invalid records reject the whole configuration."""
    data = json.loads((Path(path) if path is not None else WHITELIST_PATH).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("conversations"), list):
        raise ValueError("expected conversations list")
    identities = set()
    for item in data["conversations"]:
        if (not isinstance(item, dict) or item.get("type") not in ("group", "private")
                or type(item.get("id")) not in (str, int)
                or not re.fullmatch(r"[1-9][0-9]*", str(item["id"]))):
            raise ValueError("expected group/private and positive QQ id")
        identities.add(conversation_identity(item))
    return identities


def select_conversations(raw, conversations):
    by_index = {c["display_index"]: c for c in conversations}
    selected = []
    seen = set()
    for token in re.split(r"[,\s]+", raw.strip()):
        if not token:
            continue
        if not re.fullmatch(r"[0-9]+", token):
            raise ValueError(f"无效编号：{token}，请输入逗号或空格分隔的数字")
        index = int(token)
        if index not in by_index:
            raise ValueError(f"编号 {index} 不存在")
        conversation = by_index[index]
        identity = conversation_identity(conversation)
        if identity not in seen:
            selected.append(dict(type=identity[0], id=identity[1], name=conversation["name"]))
            seen.add(identity)
    return selected


def save_whitelist(conversations, path=None):
    path = Path(path) if path is not None else WHITELIST_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump({"conversations": conversations}, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main():
    try:
        client = _build_client()
        client.get_login_info()
        conversations = build_conversation_list(client)
        print_conversation_list(conversations)
        print("本次选择将整体替换旧白名单；留空表示不自动同步任何会话。")
        while True:
            try:
                selected = select_conversations(input("请选择需要自动同步的会话编号："), conversations)
                break
            except ValueError as exc:
                print(exc)
        print("将监听以下 QQ 会话：")
        for c in selected:
            label = "群聊" if c["type"] == "group" else "私聊"
            print(f"[{label}] {c['name']} {c['id']}")
        if not selected:
            print("（无）")
        if input("保存？(yes/no): ").strip() != "yes":
            print("已取消，旧配置未改变。")
            return 0
        save_whitelist(selected)
        print(f"已保存：{WHITELIST_PATH}")
        return 0
    except (EOFError, KeyboardInterrupt):
        print("\n已取消，旧配置未改变。")
        return 0
    except (QQClientError, OSError, ValueError, TypeError, KeyError) as exc:
        print(f"QQ 白名单配置失败：{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
