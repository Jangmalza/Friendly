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

# Copy built assets
COPY --from=builder /app/.next ./.next
COPY --from=builder /app/node_modules ./node_modules
COPY --from=builder /app/public ./public
COPY --from=builder /app/package.json ./package.json

EXPOSE 3000

ENV NODE_ENV=production

# Next.js standalone mode or standard start
CMD ["npm", "start"]
