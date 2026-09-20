#!/bin/bash
# Example SessionStart hook for loading project context
# This script detects project type and sets environment variables

set -euo pipefail

echo "Loading project context..."

# Detect project type and set environment
if [ -f "package.json" ]; then
  echo "📦 Node.js project detected"
  echo "PROJECT_TYPE=nodejs" >> ~/.nosis/.env

  # Check if TypeScript
  if [ -f "tsconfig.json" ]; then
    echo "USES_TYPESCRIPT=true" >> ~/.nosis/.env
  fi

elif [ -f "Cargo.toml" ]; then
  echo "🦀 Rust project detected"
  echo "PROJECT_TYPE=rust" >> ~/.nosis/.env

elif [ -f "go.mod" ]; then
  echo "🐹 Go project detected"
  echo "PROJECT_TYPE=go" >> ~/.nosis/.env

elif [ -f "pyproject.toml" ] || [ -f "setup.py" ]; then
  echo "🐍 Python project detected"
  echo "PROJECT_TYPE=python" >> ~/.nosis/.env

elif [ -f "pom.xml" ]; then
  echo "☕ Java (Maven) project detected"
  echo "PROJECT_TYPE=java" >> ~/.nosis/.env
  echo "BUILD_SYSTEM=maven" >> ~/.nosis/.env

elif [ -f "build.gradle" ] || [ -f "build.gradle.kts" ]; then
  echo "☕ Java/Kotlin (Gradle) project detected"
  echo "PROJECT_TYPE=java" >> ~/.nosis/.env
  echo "BUILD_SYSTEM=gradle" >> ~/.nosis/.env

else
  echo "❓ Unknown project type"
  echo "PROJECT_TYPE=unknown" >> ~/.nosis/.env
fi

# Check for CI configuration
if [ -f ".github/workflows" ] || [ -f ".gitlab-ci.yml" ] || [ -f ".circleci/config.yml" ]; then
  echo "HAS_CI=true" >> ~/.nosis/.env
fi

echo "Project context loaded successfully"
exit 0
