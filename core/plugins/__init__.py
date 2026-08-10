"""插件系统包，参照 dbcheck 的插件架构。

约定：
- 每个插件一份 JSON 清单（manifest），描述插件元数据、依赖与一键安装策略。
- 清单存放于 core/plugins/manifests/。
- core.plugin_catalog 负责加载与查询。
- core.plugin_installer 负责检测 OS、调用包管理器或下载离线包。
"""