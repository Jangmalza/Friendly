# Use the official Node.js image
FROM node:20-alpine AS builder

WORKDIR /app

# Copy package details and install
COPY package.json package-lock.json* ./
RUN npm ci

# Copy the rest of the application
COPY . .

# Build the Next.js app
RUN npm run build

# Start a new, smaller stage for running
FROM node:20-alpine AS runner

WORKDIR /app

ENV NODE_ENV=production

# Copy only standalone runtime output for a smaller image.
COPY --from=builder /app/public ./public
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static

EXPOSE 3000

CMD ["node", "server.js"]
