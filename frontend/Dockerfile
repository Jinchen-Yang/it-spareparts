# 多阶段构建：1) Node 编译静态文件；2) Nginx serve + 反代 /api
FROM node:20-alpine AS build
WORKDIR /app
# 先单独 COPY package.json 利用层缓存
COPY package.json package-lock.json* ./
RUN npm install --no-audit --no-fund
COPY . .
RUN npm run build

FROM nginx:1.27-alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
