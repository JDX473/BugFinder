package com.alibaba.flink.advisor.util;

import org.apache.commons.lang3.StringUtils;

/** 通用工具(演示外部依赖: commons-lang3 不在本仓库内)。 */
public final class ExternalUtil {

    private ExternalUtil() {
    }

    public static boolean isSinkConnError(String eventName) {
        return StringUtils.endsWith(eventName, "SINK_CONN_ERROR");
    }
}
