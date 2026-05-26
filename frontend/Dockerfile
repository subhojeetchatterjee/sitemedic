# Multi-stage build for Next.js frontend

# Stage 1: Builder
FROM node:18-alpine AS builder

ARG BUILD_DATE
ARG VCS_REF
# Firebase / agent config baked into the image at build time
ARG NEXT_PUBLIC_AGENT_URL
ARG NEXT_PUBLIC_FIREBASE_PROJECT_ID
ARG NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN
ARG NEXT_PUBLIC_FIREBASE_API_KEY
ARG NEXT_PUBLIC_FIREBASE_APP_ID

# Make build-args visible to Next.js static bundler
ENV NEXT_PUBLIC_AGENT_URL=$NEXT_PUBLIC_AGENT_URL
ENV NEXT_PUBLIC_FIREBASE_PROJECT_ID=$NEXT_PUBLIC_FIREBASE_PROJECT_ID
ENV NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN=$NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN
ENV NEXT_PUBLIC_FIREBASE_API_KEY=$NEXT_PUBLIC_FIREBASE_API_KEY
ENV NEXT_PUBLIC_FIREBASE_APP_ID=$NEXT_PUBLIC_FIREBASE_APP_ID

WORKDIR /app

# Copy package files
COPY package*.json ./

# Install dependencies
RUN npm ci

# Copy application code
COPY . .

# Build Next.js application
RUN npm run build

# Stage 2: Production runtime
FROM node:18-alpine

ARG BUILD_DATE
ARG VCS_REF

# Labels for container metadata
LABEL org.opencontainers.image.created=$BUILD_DATE
LABEL org.opencontainers.image.revision=$VCS_REF
LABEL org.opencontainers.image.title="SiteMedic Frontend"
LABEL org.opencontainers.image.source="https://github.com/your-org/sitemedic"

WORKDIR /app

# Install system dependencies
RUN apk add --no-cache curl

# Copy package files
COPY package*.json ./

# Install production dependencies only
RUN npm ci --only=production

# Copy built application from builder
COPY --from=builder /app/.next ./.next
COPY --from=builder /app/next.config.js ./next.config.js

# Create non-root user for security
RUN addgroup -g 1001 appuser && adduser -D -u 1001 -G appuser appuser
RUN chown -R appuser:appuser /app
USER appuser

# Expose port
EXPOSE 3000

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:3000 || exit 1

# Start Next.js application
CMD ["sh", "-c", "node_modules/.bin/next start -p ${PORT:-3000}"]
