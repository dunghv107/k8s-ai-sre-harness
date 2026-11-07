FROM node:22.23.4-alpine3.23 AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts && npm cache clean --force

FROM node:22.23.4-alpine3.23
# npm bundles dependencies with open CVEs and is not needed to run node
RUN rm -rf /usr/local/lib/node_modules/npm /usr/local/bin/npm /usr/local/bin/npx
WORKDIR /app
ENV NODE_ENV=production \
    PORT=8080 \
    NODE_OPTIONS="--require @opentelemetry/auto-instrumentations-node/register"
COPY --from=deps /app/node_modules ./node_modules
COPY package.json ./
COPY src ./src
USER 10001:10001
EXPOSE 8080
CMD ["node", "src/server.js"]
