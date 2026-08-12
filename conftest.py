# 全仓库共用的 pytest 启动钩子——目前只做一件事：给测试环境兜底一个签名
# 密钥，供 whatsapp_mvp/editor_token.py 用。
#
# editor_token.py 的 make_token/verify_token 是刻意 fail closed 的：
# EDITOR_TOKEN_SECRET 和 WHATSAPP_APP_SECRET 都没配置时直接抛
# EditorTokenError，绝不退化成"不校验直接放行"（模块自己的文档）。CI 跑
# 测试的容器没有任何 .env、也没有仓库级别的 secret 注入，第一次真的调用
# make_token()/verify_token() 的测试会直接把这层安全设计"撞"出一个失败——
# 真实复现：2026-08-12，PR #61 CI 上 11 个编辑器导出路由测试全部因为这个
# 原因失败，本机开发环境因为 `.env` 里已经配了 WHATSAPP_APP_SECRET，从没
# 暴露过这个缺口。
#
# 用 os.environ.setdefault 而不是直接赋值——只在环境里还没有真实密钥时才
# 填一个测试专用值，不会覆盖开发者本机 `.env` 已经加载好的真实密钥（config.py
# 的 Config 字段用 default_factory=lambda: os.getenv(...) 在 Config() 实例化
# 那一刻读取一次，pytest 保证 conftest.py 在收集/导入任何测试模块之前先执行，
# 所以这里设置永远早于任何测试第一次触发 get_config()）。
import os

os.environ.setdefault("WHATSAPP_APP_SECRET", "test-only-secret-do-not-use-in-prod")
