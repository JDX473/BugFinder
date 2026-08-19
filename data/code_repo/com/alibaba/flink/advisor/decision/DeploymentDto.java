package com.alibaba.flink.advisor.decision;

/** 部署信息 DTO: 决策服务的输入对象。 */
public class DeploymentDto {

    private String deploymentId;
    private String jobId;

    public String getDeploymentId() {
        return deploymentId;
    }

    public String getJobId() {
        return jobId;
    }
}
