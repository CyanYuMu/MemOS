package localsync

import (
	"bytes"
	"compress/gzip"
	"errors"
	"fmt"
	"io"
)

// compress.go 放 payload 的解压与详情读取。入站压缩在 ingest.go，这里只处理镜像存储。

var errPayloadTooLarge = errors.New("payload exceeds display limit")

// gzipReader 只接受 gzip 流；调用方负责 Close。
func gzipReader(body []byte) (*gzip.Reader, error) {
	reader, err := gzip.NewReader(bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("localsync: decode payload gzip: %w", err)
	}
	return reader, nil
}

// readAllLimited 读取解压后的全部字节，超过 max 返回错误。
func readAllLimited(reader io.Reader, maxBytes int) ([]byte, error) {
	data, err := io.ReadAll(io.LimitReader(reader, int64(maxBytes)+1))
	if err != nil {
		return nil, fmt.Errorf("localsync: read payload: %w", err)
	}
	if len(data) > maxBytes {
		return nil, errPayloadTooLarge
	}
	return data, nil
}

// decompressPayload 解压镜像 payload 并解析为 JSON 对象。
func decompressPayload(body []byte) (map[string]any, error) {
	if len(body) == 0 {
		return map[string]any{}, nil
	}
	gz, err := gzipReader(body)
	if err != nil {
		return nil, err
	}
	defer func() { _ = gz.Close() }()
	raw, err := readAllLimited(gz, maxPayloadDetailBytes)
	if err != nil {
		return nil, err
	}
	payload, reason := decodePayload(raw)
	if reason != "" {
		return nil, errors.New(reason)
	}
	return payload, nil
}
