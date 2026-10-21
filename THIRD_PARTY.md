# 第三方依赖与参考

依赖通过 `requirements.txt` 安装，本仓库不附带依赖副本或微信、模型文件。相关许可与版权归各项目所有。

| 项目 | 用途 |
| --- | --- |
| [Pillow](https://github.com/python-pillow/Pillow) | 图片处理与界面截图 |
| [uiautomation](https://github.com/yinkaisheng/Python-UIAutomation-for-Windows) | Windows UI Automation |
| [comtypes](https://github.com/enthought/comtypes) | COM 支持 |
| [PyWinRT](https://github.com/pywinrt/pywinrt) | Windows Runtime 与系统 OCR |
| [LM Studio](https://lmstudio.ai/) | 本地模型 API 与模型加载管理，需单独安装 |

群聊节奏设计参考了 [AstrBot Group Chat Plus](https://github.com/Him666233/astrbot_plugin_group_chat_plus) 的注意力与对话疲劳思路；微信界面消息类型映射参考了 [pywechat / pyweixin](https://github.com/Hello-Mr-Crab/pywechat)。这些项目不作为本仓库运行依赖，也不随仓库分发。

搜索模块直接请求 DuckDuckGo HTML 和 Bing 页面，不使用第三方搜索 SDK。搜索结果和用户自备表情的内容权利不因本仓库的分发而改变。
