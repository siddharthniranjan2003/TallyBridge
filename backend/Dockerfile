# Cloud Run image for the TallyBridge TEST backend (Express + Supabase + Firebase).
# Compiles TypeScript to dist/ then runs `node dist/index.js`.
# Cloud Run injects $PORT (8080); index.ts already reads process.env.PORT.
FROM node:20-slim

WORKDIR /app

# Install deps first for layer caching. devDeps (typescript) are needed for the build.
COPY package*.json ./
RUN npm install

COPY . .
RUN npm run build

ENV NODE_ENV=production
CMD ["npm", "start"]
