# syntax=docker/dockerfile:1.4
# ===================================
# Base Image for All Python Services
# ===================================
# This base image provides common dependencies to reduce build times
# and ensure consistency across all services.

FROM python:3.12-slim AS base

# Common system dependencies used by multiple services
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    openssh-client \
    && rm -rf /var/lib/apt/lists/*

# Set Python environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
