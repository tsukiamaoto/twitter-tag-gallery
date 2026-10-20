"""Local MCP policy and client configuration; model login belongs to the client."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from background_work import atomic_json, FileLock

from tool_registry import TOOLS

DEFAULTS = {"mcp_enabled":True,"mcp_read_only":False,"disabled_tools":[]}
CLIENT_SERVER_NAME = "gallery"  # FQN mcp_<server>_<tool> must fit model tool-name limits.


def config_path():
    from database import DATA_DIR
    return Path(DATA_DIR)/"integrations.json"


def load():
    path = config_path()
    if not path.exists():
        return dict(DEFAULTS,disabled_tools=[])
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value,dict) or any(not isinstance(value.get(key,True),bool) for key in ("mcp_enabled","mcp_read_only")):
            raise ValueError("Invalid MCP policy")
        if not isinstance(value.get("disabled_tools",[]),list) or any(not isinstance(name,str) for name in value.get("disabled_tools",[])):
            raise ValueError("Invalid MCP tools")
        return {key:value.get(key,default) for key,default in DEFAULTS.items()}
    except (OSError,ValueError):
        # Malformed policy must never enable writes accidentally.
        return {**DEFAULTS,"mcp_enabled":False,"config_error":"設定檔無法讀取，請重新儲存"}



def public_config():
    config = load()
    result = {key:config.get(key) for key in DEFAULTS}
    result["config_error"] = config.get("config_error","")
    result["tools"] = [{"name":name,"label":label,"writes":writes,"enabled":allowed(name,config)} for name,(label,writes) in TOOLS.items()]
    result["transport"] = "stdio"
    return result


def save(values):
    if set(values) - set(DEFAULTS):
        raise ValueError("僅支援 MCP 設定；模型登入請在 Antigravity 或 Gemini CLI 管理")
    lock = FileLock(config_path().with_suffix(".lock"))
    if not lock.acquire():
        raise ValueError("其他設定正在儲存，請稍後重試")
    try:
        config = load()
        for name in ("mcp_enabled","mcp_read_only"):
            if name in values:
                if not isinstance(values[name],bool):
                    raise ValueError(f"{name} 必須為布林值")
                config[name] = values[name]
        if "disabled_tools" in values:
            tools = values["disabled_tools"]
            if not isinstance(tools,list) or any(name not in TOOLS for name in tools):
                raise ValueError("包含未知的 MCP 工具")
            config["disabled_tools"] = sorted(set(tools))
        config.pop("config_error",None)
        atomic_json(config_path(),config,durable=True)
    finally:
        lock.release()
    return public_config()


def allowed(name,config=None):
    config = config or load()
    return bool(config["mcp_enabled"] and name in TOOLS and name not in config["disabled_tools"] and not(config["mcp_read_only"] and TOOLS[name][1]))


def require_tool(name):
    if not allowed(name):
        raise PermissionError("此 MCP 工具已停用或受唯讀模式限制")


def client_config(client="gemini_cli"):
    import database
    if client not in ("antigravity", "gemini_cli"):
        raise ValueError("不支援的 MCP 客戶端")
    config = load()
    server = {
        "command":sys.executable,"args":[str(Path(__file__).with_name("mcp_server.py"))],
        "cwd":str(Path(__file__).parent),"env":{"TWITTER_GALLERY_ROOT":str(database.GALLERY_ROOT)},
    }
    if client == "antigravity":
        server.update(disabled=not config["mcp_enabled"], disabledTools=[name for name in TOOLS if not allowed(name,config)])
    else:
        server.update(timeout=600000, trust=False, includeTools=[name for name in TOOLS if allowed(name,config)])
    return {"mcpServers":{CLIENT_SERVER_NAME:server}}


def record_session(action,tool=None):
    from database import DATA_DIR
    directory = Path(DATA_DIR)/"mcp_sessions"
    atomic_json(directory/f"{os.getpid()}.json",{"pid":os.getpid(),"last_seen":time.time(),"action":action,"tool":tool})


def sessions():
    from database import DATA_DIR
    result=[]
    for path in (Path(DATA_DIR)/"mcp_sessions").glob("*.json"):
        try:
            value=json.loads(path.read_text(encoding="utf-8"))
            if time.time()-value["last_seen"]<86400:
                result.append(value)
        except (OSError,ValueError,KeyError):
            continue
    return sorted(result,key=lambda item:item["last_seen"],reverse=True)[:20]


def diagnose_mcp():
    requests = [
        {"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"gallery-settings-diagnostic","version":"1.0"}}},
        {"jsonrpc":"2.0","method":"notifications/initialized"},
        {"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}},
    ]
    env=os.environ.copy()
    env.update(PYTHONIOENCODING="utf-8",TWITTER_GALLERY_MCP_DIAGNOSTIC="1")
    import database
    env["TWITTER_GALLERY_ROOT"]=str(database.GALLERY_ROOT)
    try:
        result=subprocess.run([sys.executable,"-B",str(Path(__file__).with_name("mcp_server.py"))],input="".join(json.dumps(req)+"\n" for req in requests),capture_output=True,text=True,encoding="utf-8",timeout=15,env=env,
                              creationflags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0)
        responses=[json.loads(line) for line in result.stdout.splitlines() if line.strip().startswith("{")]
        tool_response=next((response for response in responses if response.get("id")==2),{})
        tools=tool_response.get("result",{}).get("tools")
        if tools is None:
            return {"ok":False,"message":"MCP 啟動或握手失敗，請檢查 Python 與套件環境","exit_code":result.returncode}
        return {"ok":True,"message":f"本機 MCP 握手成功，可用工具 {len(tools)} 個","tool_count":len(tools),"tools":[tool["name"] for tool in tools]}
    except subprocess.TimeoutExpired:
        return {"ok":False,"message":"MCP 連線診斷逾時（15 秒）"}
