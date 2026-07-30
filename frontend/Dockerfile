# 多阶段构建：1) Node 编译静态文件；2) Nginx serve + 反代 /api
FROM --platform=linux/amd64 node:20-alpine@sha256:afdf98210b07b586eb71fa22ba2e432e058e4cd1304d31ed60888755b8c865fb AS build
WORKDIR /app
# 先单独 COPY package.json 利用层缓存
# npm 源可由构建参数覆盖（国内服务器走镜像，默认上游 npmjs）
ARG NPM_REGISTRY=https://registry.npmjs.org
COPY package.json package-lock.json ./
RUN npm config set registry ${NPM_REGISTRY} && npm ci --no-audit --no-fund
COPY . .
RUN npm run build

FROM --platform=linux/amd64 nginx:1.27-alpine@sha256:62223d644fa234c3a1cc785ee14242ec47a77364226f1c811d2f669f96dc2ac8
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
