FROM mcr.microsoft.com/vscode/devcontainers/python:3.11

ENV PYTHONUNBUFFERED 1

# Install PostgreSQL client
RUN apt-get update && export DEBIAN_FRONTEND=noninteractive \
    && apt-get -y install --no-install-recommends postgresql-client
