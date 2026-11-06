---
module: plugin
packages:
  - internal/transport/plugin
status: implemented
---

# Plugin artifact hosting

The optional endpoint serves explicitly configured plugin artifacts. This distribution does not include plugin artifacts.

## 职责与边界

Serve allowed static artifact files; artifact creation and installation are outside this module.

## 依赖

The application configures an optional artifact directory and mounts the handler.

## 契约

Only allowed filenames and read methods are served. Versioned archives use immutable caching.

## 错误与边界情况

An unconfigured directory or an unsupported filename returns 404.

## 测试与验证

Unit tests in `internal/transport/plugin` cover path restrictions, methods and responses.

## 参考来源

`internal/transport/plugin/plugin.go` and its tests define the current behavior.

## 实现记录

See the public version-control history.
