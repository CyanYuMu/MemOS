# 生产目标：只含静态 memosd（以及可选的控制台前端产物）。
FROM scratch AS prod
# 版本出处：后端 commit 来自 make build-linux 的 ldflags，前端来自 web/dist/frontend.json。
# 前端是另一个仓库，镜像里只有产物、没有它的 git 信息，所以这里把两边都打在 LABEL 上，
# 线上究竟是哪版前端可以 `docker inspect` 直接看到，不必再比对 asset hash。
ARG BACKEND_REVISION=unknown
ARG FRONTEND_REVISION=unknown
ARG FRONTEND_BRANCH=unknown
LABEL org.opencontainers.image.revision="$BACKEND_REVISION" \
      console.frontend.revision="$FRONTEND_REVISION" \
      console.frontend.branch="$FRONTEND_BRANCH"
# 生产镜像里二进制叫 memory-console，和它承担的角色一致；源码目录仍叫 cmd/memosd。
COPY bin/memosd-linux-amd64 /memory-console
# 聊天页的 agent runtime：pi 是 musl 静态二进制，子进程方式驱动（见 docs/module/rust-pi-agent.md）。
COPY bin/pi-linux-amd64 /pi
# pi 用 rustls 的系统根证书做 HTTPS；scratch 里没有证书库，构建前由 make ca 放进 bin。
COPY bin/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    MEMOSD_CHAT_PI_BIN=/pi
# 前端产物在构建前放到 web/dist；目录为空也能 COPY，是否启用由 MEMOSD_WEB_DIR 决定。
COPY web/ /app/web/
USER 65532:65532
EXPOSE 5280 8002
ENTRYPOINT ["/memory-console"]
