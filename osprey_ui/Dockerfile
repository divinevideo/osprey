FROM node:24-alpine

WORKDIR /app

COPY osprey_ui/package.json osprey_ui/package-lock.json* ./

RUN npm ci --legacy-peer-deps

COPY osprey_ui/ .

EXPOSE 5002

CMD ["npm", "start"]
