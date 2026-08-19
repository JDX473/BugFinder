package com.alibaba.flink.advisor.mapper;

import com.alibaba.flink.advisor.entity.FlinkLifecycle;

import java.util.List;

/**
 * 数据访问层: 按 deployment ID / job ID 检索 Flink 作业的生命周期事件。
 */
public interface FlinkLifecycleMapper {

    long countEvents(String deploymentId, String jobId, String eventName, long windowMs);

    List<FlinkLifecycle> findEvents(String deploymentId, String jobId, long windowMs);
}
