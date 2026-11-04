# 依赖与第三方说明

本项目自身代码采用根目录的MIT许可证。第三方组件继续遵循各自许可证，项目名称、平台名称和真实商品资料的权利分别归属其权利人。

## 直接依赖

以下许可名称根据本机已安装依赖的包元数据核对；具体版本见锁文件，完整许可文本由各组件的发行包提供。

- React与React DOM：MIT；[官方仓库](https://github.com/facebook/react)。
- Lucide React：ISC；[官方仓库](https://github.com/lucide-icons/lucide)。
- Vite与React插件：MIT；[Vite仓库](https://github.com/vitejs/vite)、[插件仓库](https://github.com/vitejs/vite-plugin-react)。
- TypeScript：Apache-2.0；[官方仓库](https://github.com/microsoft/TypeScript)。
- FastAPI：MIT；[官方仓库](https://github.com/fastapi/fastapi)。
- Uvicorn：BSD-3-Clause；[官方仓库](https://github.com/Kludex/uvicorn)。
- Playwright Python：Apache-2.0；[官方仓库](https://github.com/microsoft/playwright-python)。
- Pydantic：MIT；[官方仓库](https://github.com/pydantic/pydantic)。
- Starlette：BSD-3-Clause；[官方仓库](https://github.com/Kludex/starlette)。
- HTTPX：BSD-3-Clause；[官方仓库](https://github.com/encode/httpx)。
- pytest：MIT；[官方仓库](https://github.com/pytest-dev/pytest)。

Python环境和前端工具还会安装间接依赖，请保留其发行包自带的许可证。本项目的源码包不捆绑`.venv/`、`node_modules/`或`dist/`；若另行分发包含第三方代码的构建产物，应同时保留适用的版权和许可声明。

## 示例资料和图片

`public/demo-assets/`中的SVG为本项目绘制的原创演示插图；演示截图只使用虚构商品、供应商、价格和销量。原始平台页面截图、真实业务数据库及浏览器资料不纳入源码展示包。

## 参考项目

公开阅读过的采集示例和设计资料见[资料与项目参考](docs/开源参考.md)。它们作为设计参考记录，不作为当前运行依赖；没有直接复制其中未核实许可的实现。
