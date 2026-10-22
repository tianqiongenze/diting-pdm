# 静态二进制 + distroless：镜像 ~10MB，无 shell、无包管理器，攻击面最小
# 构建（在 impl/ 下）：
#   CGO_ENABLED=0 go build -o eventpipe ./services/go-eventpipe
#   docker build -t <registry>/pdm/go-eventpipe:0.1.0 -f services/go-eventpipe/Dockerfile .
FROM 10.167.2.175:30500/base/distroless-static:nonroot

COPY eventpipe /eventpipe
USER 65532:65532
EXPOSE 8080
ENTRYPOINT ["/eventpipe"]
