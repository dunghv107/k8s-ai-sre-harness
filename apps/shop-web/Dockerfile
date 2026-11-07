FROM node:22.23.4-alpine3.23 AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts && npm cache clean --force
COPY . .
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build

FROM node:22.23.4-alpine3.23
# npm bundles dependencies with open CVEs and is not needed to run node
RUN rm -rf /usr/local/lib/node_modules/npm /usr/local/bin/npm /usr/local/bin/npx
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0
COPY --from=build /app/.next/standalone ./
COPY --from=build /app/.next/static ./.next/static
COPY --from=build /app/public ./public
# the runtime cache lives under .next/cache, so link it to the writable /tmp
RUN ln -s /tmp/next-cache /app/.next/cache && chmod -R a+rX /app
USER 10001
EXPOSE 3000
CMD ["node", "server.js"]
