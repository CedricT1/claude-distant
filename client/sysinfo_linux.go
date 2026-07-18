//go:build linux

package main

import (
	"bufio"
	"fmt"
	"os"
	"strconv"
	"strings"
)

func platformGetUptimeSeconds() (int64, error) {
	f, err := os.Open("/proc/uptime")
	if err != nil {
		return 0, err
	}
	defer f.Close()

	buf := make([]byte, 64)
	n, err := f.Read(buf)
	if err != nil && n == 0 {
		return 0, err
	}
	fields := strings.Fields(string(buf[:n]))
	if len(fields) == 0 {
		return 0, fmt.Errorf("format /proc/uptime inattendu")
	}
	seconds, err := strconv.ParseFloat(fields[0], 64)
	if err != nil {
		return 0, err
	}
	return int64(seconds), nil
}

func platformGetMemoryMB() (totalMB, availMB uint64, err error) {
	f, err := os.Open("/proc/meminfo")
	if err != nil {
		return 0, 0, err
	}
	defer f.Close()

	values := map[string]uint64{}
	scanner := bufio.NewScanner(f)
	for scanner.Scan() {
		parts := strings.Fields(scanner.Text())
		if len(parts) < 2 {
			continue
		}
		key := strings.TrimSuffix(parts[0], ":")
		if key != "MemTotal" && key != "MemAvailable" {
			continue
		}
		kb, convErr := strconv.ParseUint(parts[1], 10, 64)
		if convErr != nil {
			continue
		}
		values[key] = kb
	}
	if err := scanner.Err(); err != nil {
		return 0, 0, err
	}
	return values["MemTotal"] / 1024, values["MemAvailable"] / 1024, nil
}
