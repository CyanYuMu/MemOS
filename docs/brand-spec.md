# Playground 复刻参考

- 参考页面：https://memos-playground.openmem.net/cn/main/service/
- 调研日期：2026-10-31。
- 观察来源：已登录页面截图、公开 Nuxt 样式与教程配置；未复制认证、分析上报或远端 API 调用代码。
- 设计基线：`#f0f2f5` 侧栏与顶部、`#ffffff` 对话面板、`#f7f7ff` 空态浅色、`#4044ed` 主色、`#e0dffd` 日志边线。暗色由聊天区域局部语义变量配套。
- 字体：参考页的 Helvetica Neue / PingFang SC 无衬线栈；控制台正文 14px、聊天正文 16px/28px、辅助信息 10–12px、空态标题 24px。
- 几何：复用控制台 220px 侧栏（收起 68px）；聊天局部工具栏 56px；日志 300–400px；空态输入框最大 700px；中间区域自适应；小于 1100px 日志改为抽屉，小于 720px 共用导航改为抽屉。保留已删除全局顶栏的决定。
- 引导：445px 紫蓝渐变欢迎卡、左上角机器人装饰、700px 偏好表单；小屏宽度不超过视口减 32px。聚光指引用相同的渐变、机器人和白色主按钮。
- 日志动效：参考原站的 0.6s 入场/位置移动、2s 新日志高亮、1.5s 状态脉冲和 3.5 倍扩散；支持减少动态效果。
- 品牌：按用户要求使用 Apache HugeGraph 官方图形标识，来源为 `https://hugegraph.apache.org/img/logo.svg`，保存到 `/logo.svg`；侧栏、手机导航、聊天头像和 favicon 共用。保留 Graph Memory 应用名称。
- 欢迎页图片：`public/playground/robot.png`，来自参考站公开资源 `https://memos-playground.openmem.net/_nuxt/airobot.uR98C7Y_.png`，用于本次复刻。
- 引导依赖：Driver.js 1.10.2，精确锁版本；官方文档 https://driverjs.com/docs/installation 与 https://driverjs.com/docs/configuration 。库按需加载。

- MemCube 侧栏：使用原站渲染 DOM 内嵌的 WebP 背景，保存到 `public/playground/memcube-wash.webp`；400px 宽、20px 内边距、16px 左侧圆角，日志卡片 6px 圆角/8px 内边距，标题 14px、摘要 12px/16px 行高。源码和 SHA-256 清单保留在任务 reference 目录。

- 界面与产品文案统一使用 Graph Memory，包括欢迎引导、空态和错误提示；不出现原站品牌。中英字典及可见页面均扫描。

- Logo 的页面图形与 favicon 共用 `/logo.svg?v=hugegraph` 版本地址，避免旧浏览器缓存保留上一品牌图形；更换图形内容时同步更新资源版本标识。

- 真实聊天页复核：用户气泡使用原站 `#eff0ff`、16px内边距与12/12/0/12圆角；回答正文16px/28px，白色阅读背景。思考和工具过程合并为260px紧凑入口，完成后折叠，点击或日志定位可展开。
- 日志展示实际对话/记忆内容；异步受理不冒充已提取条目。不在摘要和业务详情里显示回执JSON，记忆类型、状态、标签、更新时间只在实际返回时显示；无时间数据不生成时间。
