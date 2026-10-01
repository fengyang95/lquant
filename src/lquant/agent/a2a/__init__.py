"""A2A（Agent2Agent）协议层：让外部 Agent 以标准协议调用内置 Claude Code。

本包只做协议适配，不重复实现对话：执行体仍是 ``AgentService``
（provider=claude_code），会话事实源仍是 ``ask_sessions``/``ask_messages``。
"""
