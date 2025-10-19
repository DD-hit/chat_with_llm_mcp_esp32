import asyncio
from contextlib import AsyncExitStack
from typing import Any, Dict, List

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from zhipuai import ZhipuAI
from dotenv import load_dotenv
import json

load_dotenv()  # 从 .env 加载环境变量（例如 ZHIPUAI_API_KEY）

# ------------------------------
# 常量配置（集中放置便于查找）
# ------------------------------
DEFAULT_CONFIG_PATH = "mcp_config.json"        # 默认的配置文件路径
DEFAULT_MODEL = "glm-4-flash"                  # 智谱推理模型（与 self.zhipu 调用保持一致）
class LLM_MCP:
    """MCP 多服务器客户端

    主要职责：
    - 从配置文件加载各个 MCP 服务器的启动参数（脚本、类型、参数、环境变量）。
    - 为每个服务器启动独立的 stdio 子进程，建立 JSON-RPC/MCP 会话。
    - 聚合所有服务器的工具，供 LLM 工具调用统一路由。
    - 提供交互式 chat 循环，与 LLM 配合根据需要调用工具。
    """

    def __init__(self, config_path: str = DEFAULT_CONFIG_PATH, system_prompt: str = None):
        # 维护所有已连接服务器的会话：key=服务器名（来自配置），value=ClientSession
        self.sessions: Dict[str, ClientSession] = {}
        # 统一的异步资源托管器，确保子进程/会话按正确顺序关闭，避免泄露
        self.exit_stack = AsyncExitStack()
        # 智谱 AI 客户端，用于生成回复 & 触发工具调用
        self.zhipu = ZhipuAI()
        # 配置文件路径与内容
        self.config_path = config_path
        self.config = self._load_config()
        self.system_prompt = system_prompt

    def _load_config(self) -> dict:
        """加载MCP服务器配置文件"""
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except FileNotFoundError:
            print(f"配置文件 {self.config_path} 不存在，将使用空配置")
            return {"servers": {}, "default_servers": []}
        except json.JSONDecodeError as e:
            raise ValueError(f"配置文件格式错误: {e}")

    # ------------------------------
    # 内部小工具：配置、类型检测、参数构建与会话启动
    # ------------------------------
    def _get_server_config(self, server_name: str) -> Dict[str, Any]:
        """获取并做轻量校验的服务器配置。"""
        servers = self.config.get("servers", {})
        if server_name not in servers:
            raise ValueError(f"服务器 '{server_name}' 未在配置文件中找到")
        cfg = servers[server_name]
        # 基础字段校验与默认
        if "script_path" not in cfg:
            raise ValueError(f"服务器 '{server_name}' 缺少必须字段: script_path")
        if not isinstance(cfg.get("args", []), list):
            raise ValueError(f"服务器 '{server_name}' 的 args 必须是数组(list)")
        if not isinstance(cfg.get("env", {}), dict):
            raise ValueError(f"服务器 '{server_name}' 的 env 必须是字典(dict)")
        return cfg

    def _detect_server_type(self, script_path: str, declared_type: str) -> str:
        """根据声明或脚本后缀确定服务器类型（python/node）。"""
        st = (declared_type or "auto").lower()
        if st in ("python", "node"):
            return st
        if st == "auto":
            lower = script_path.lower()
            if lower.endswith('.py'):
                return "python"
            if lower.endswith('.js'):
                return "node"
            raise ValueError(f"无法自动检测服务器类型: {script_path}")
        raise ValueError(f"不支持的服务器类型: {declared_type}（仅支持 python/node/auto）")

    def _build_server_params(self, script_path: str, server_type: str, server_args: List[str], env: Dict[str, str]) -> StdioServerParameters:
        """基于类型与脚本构造 stdio 启动参数对象。"""
        command = "python" if server_type == "python" else "node"
        return StdioServerParameters(
            command=command,
            args=[script_path, *server_args],  # 列表传参更安全，避免 shell 转义问题
            env=env
        )

    async def _start_session(self, server_name: str, server_params: StdioServerParameters) -> ClientSession:
        """启动子进程并创建会话，完成 MCP 初始化握手。"""
        # 1) 启动 stdio 传输（子进程），资源交给 exit_stack 管理
        stdio, write = await self.exit_stack.enter_async_context(stdio_client(server_params))
        # 2) 创建 MCP 协议会话，同样交给 exit_stack 管理
        session = await self.exit_stack.enter_async_context(ClientSession(stdio, write))
        # 3) 初始化握手（失败多半是服务端脚本异常或 stdout 打印日志污染协议）
        await session.initialize()
        # 4) 记录会话，便于后续路由工具调用
        self.sessions[server_name] = session
        return session

    async def connect_to_server(self, server_name: str, server_config: dict = None):
        """连接到 MCP 服务器

        Args:
            server_name: 服务器名称
            server_config: 服务器配置（如果不提供，从配置文件读取）
        """
        try:
            # 1) 获取并校验配置
            server_config = server_config or self._get_server_config(server_name)
            script_path: str = server_config["script_path"]
            declared_type: str = server_config.get("type", "auto")
            server_args: List[str] = server_config.get("args", [])
            env: Dict[str, str] = server_config.get("env", {})

            # 2) 确定服务器类型（python/node）
            server_type = self._detect_server_type(script_path, declared_type)

            # 3) 构造 stdio 启动参数
            server_params = self._build_server_params(script_path, server_type, server_args, env)

            # 4) 启动子进程并初始化 MCP 会话
            session = await self._start_session(server_name, server_params)

            # 5) 列出可用工具（仅作为连接成功反馈）
            response = await session.list_tools()
            tools = response.tools
            print(f"\n已连接到服务器 '{server_name}'，工具包括：{[tool.name for tool in tools]}")

        except Exception as e:
            # 常见原因提示，方便排查服务器侧问题
            details = (
                f"连接服务器 '{server_name}' 失败。\n"
                "可能原因：\n"
                "- 服务器脚本在启动后崩溃或提前退出（依赖未安装/异常）。\n"
                "- 服务器向 stdout 打印了日志，破坏了 MCP JSON-RPC 协议（应将日志输出到 stderr）。\n"
                "- 服务器脚本需要额外参数（如串口号、端口、API KEY 等），未提供导致退出。\n"
                "- 服务器并未实现 MCP 协议的 stdio 服务。\n"
                "- 权限或环境变量问题（串口/设备访问、PATH、PYTHONPATH）。\n"
                f"配置: {server_config}"
            )
            raise RuntimeError(details) from e

    async def connect_all_default_servers(self):
        """连接配置文件中指定的默认服务器"""
        default_servers = self.config.get("default_servers", [])
        if not default_servers:
            print("未指定默认服务器")
            return
            
        for server_name in default_servers:
            try:
                await self.connect_to_server(server_name)
            except Exception as e:
                print(f"连接服务器 '{server_name}' 失败: {e}")

    async def connect_all_servers(self):
        """连接配置文件中的所有服务器"""
        for server_name in self.config["servers"]:
            try:
                await self.connect_to_server(server_name)
            except Exception as e:
                print(f"连接服务器 '{server_name}' 失败: {e}")

    async def list_all_tools(self):
        """获取所有连接服务器的工具列表"""
        all_tools = []
        for server_name, session in self.sessions.items():
            try:
                response = await session.list_tools()
                for tool in response.tools:
                    # 为工具名添加服务器前缀，避免冲突
                    tool_with_prefix = {
                        "name": f"{server_name}:{tool.name}",
                        "description": f"[{server_name}] {tool.description}",
                        "inputSchema": tool.inputSchema,
                        "server_name": server_name,
                        "original_name": tool.name
                    }
                    all_tools.append(tool_with_prefix)
            except Exception as e:
                print(f"获取服务器 '{server_name}' 工具列表失败: {e}")
        return all_tools

    async def call_tool_by_name(self, tool_name: str, args: dict):
        """根据工具名调用对应服务器的工具"""
        # if ":" in tool_name:
        server_name, original_tool_name = tool_name.split(":", 1)
        # else:
        #     # 兼容没有前缀的情况，使用第一个可用服务器
        #     if not self.sessions:
        #         raise ValueError("没有可用的服务器连接")
        #     server_name = next(iter(self.sessions.keys()))
        #     original_tool_name = tool_name
            
        if server_name not in self.sessions:
            raise ValueError(f"服务器 '{server_name}' 未连接")
            
        session = self.sessions[server_name]
        return await session.call_tool(original_tool_name, args)

    async def process_query(self, query: str) -> str:
        """
        使用 GLM-4-Flash 和可用的工具处理查询
        支持 system prompt（官方推荐用 role='system' 消息）
        """
        if not self.sessions:
            return "错误：没有连接到任何MCP服务器。请检查配置文件。"

        messages = []
        # 官方推荐：system prompt 放在第一条消息
        if self.system_prompt:
            messages.append({
                "role": "system",
                "content": self.system_prompt
            })
        messages.append({
            "role": "user",
            "content": query
        })

        all_tools = await self.list_all_tools()
        available_tools = [{
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["inputSchema"]
            }
        } for tool in all_tools]

        response = self.zhipu.chat.completions.create(
            model=DEFAULT_MODEL,
            max_tokens=1000,
            messages=messages,
            tools=available_tools
        )

        message = response.choices[0].message

        # 工具调用循环，直到没有 tool_calls
        while hasattr(message, 'tool_calls') and message.tool_calls:
            for tool_call in message.tool_calls:
                tool_name = tool_call.function.name
                tool_args = json.loads(tool_call.function.arguments) if isinstance(tool_call.function.arguments, str) else tool_call.function.arguments
                result = await self.call_tool_by_name(tool_name, tool_args)
                # 工具调用结果加入对话历史
                messages.append({
                    "role": "assistant",
                    "content": message.content or "",
                    "tool_calls": [{
                        "id": tool_call.id,
                        "type": "function",
                        "function": {
                            "name": tool_call.function.name,
                            "arguments": tool_call.function.arguments
                        }
                    }]
                })
                messages.append({
                    "role": "tool",
                    "content": str(result.content),
                    "tool_call_id": tool_call.id
                })
                # 再次请求大模型
                response = self.zhipu.chat.completions.create(
                    model=DEFAULT_MODEL,
                    max_tokens=1000,
                    messages=messages,
                    tools=available_tools
                )
                message = response.choices[0].message

        # 只返回最后一次大模型回复内容
        return message.content or ""

    async def chat_loop(self):
        """运行交互式聊天循环"""
        print("\nMCP 客户端已启动！")
        print("输入你的查询或输入 'quit' 退出。")

        while True:
            try:
                query = input("\n查询: ").strip()

                if query.lower() == 'quit':
                    break

                response = await self.process_query(query)
                print("\n" + response)

            except Exception as e:
                print(f"\n错误: {str(e)}")

    async def cleanup(self):
        """清理资源"""
        await self.exit_stack.aclose()

async def main():
    import argparse
    
    parser = argparse.ArgumentParser(
        description="MCP智能客户端 - 支持多服务器连接和智能工具调用",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例:
  python client.py                    # 连接默认配置的服务器
  python client.py --all              # 显式连接所有配置的服务器
  python client.py -s led_controller  # 仅连接LED控制器
  python client.py -s led -s weather  # 连接LED和天气服务器
  python client.py -c my_config.json  # 使用自定义配置文件
        """
    )
    parser.add_argument("--config", "-c", 
                       default="mcp_config.json", 
                       help="MCP服务器配置文件路径 (默认: mcp_config.json)")
    parser.add_argument("--server", "-s", 
                       action="append", 
                       metavar="NAME",
                       help="指定要连接的服务器名称，可多次使用仅连接特定服务器")
    parser.add_argument("--all", "-a", 
                       action="store_true", 
                       help="连接所有配置的服务器（与默认行为相同，显式指定）")
    args = parser.parse_args()

    client = LLM_MCP(config_path=args.config)
    try:
        if args.server:
            print(f"连接指定的服务器: {args.server}")
            for server_name in args.server:
                await client.connect_to_server(server_name)
        elif args.all:
            print("连接所有配置的服务器 (--all)...")
            await client.connect_all_servers()
        else:
            # 默认连接所有配置的服务器
            print("连接所有配置的服务器...")
            await client.connect_all_default_servers()
        
        if not client.sessions:
            print("未连接到任何服务器，退出。")
            return
            
        await client.chat_loop()
    finally:
        await client.cleanup()

if __name__ == "__main__":
    import sys
    asyncio.run(main())