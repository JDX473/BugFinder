package com.alibaba.flink.advisor.entity;

/** Flink 作业生命周期事件实体(事件名、发生时间、关联作业)。 */
public class FlinkLifecycle {

    private String eventName;
    private long occurredAt;
    private String deploymentId;
    private String jobId;

    public String getEventName() {
        return eventName;
    }

    public long getOccurredAt() {
        return occurredAt;
    }

    public String getDeploymentId() {
        return deploymentId;
    }

    public String getJobId() {
        return jobId;
    }
}
