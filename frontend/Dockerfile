FROM node:20-alpine AS builder

WORKDIR /app

COPY package*.json ./

RUN npm ci

COPY . .

# A deployed build always uses the real API, through the nginx /api/ proxy (frontend/nginx.conf).
ARG VITE_USE_MOCK=false
ARG VITE_API_BASE_URL=/api
ENV VITE_USE_MOCK=${VITE_USE_MOCK}
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL}

RUN npm run build


FROM nginxinc/nginx-unprivileged:alpine AS runtime

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /app/dist /usr/share/nginx/html

EXPOSE 8080

CMD ["nginx", "-g", "daemon off;"]
