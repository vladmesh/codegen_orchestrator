FROM node:22.23.2-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM nginx:1.31.3-alpine
RUN apk add --no-cache apache2-utils
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
EXPOSE 80
ENTRYPOINT ["/entrypoint.sh"]

# Which tree this image was built from; the service image release refuses a candidate
# whose label is not the tree's hash (infra/scripts/publish-service-images.sh).
ARG SOURCE_HASH=unknown
LABEL org.codegen.worker_source_hash=$SOURCE_HASH
