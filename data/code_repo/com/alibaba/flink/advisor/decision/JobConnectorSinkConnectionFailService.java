package com.alibaba.flink.advisor.decision;

/**
 * 诊断服务: 判定 Flink 作业的连接器 Sink 连接失败(对齐论文图4 示例类)。
 * 检查是否存在最近 300000ms 内、事件名以 SINK_CONN_ERROR 结尾的
 * FlinkLifecycle 事件;命中则返回决策值 1。
 */
public class JobConnectorSinkConnectionFailService extends RuleDecisionBase {

    private final FlinkLifecycleMapper lifecycleMapper;

    public JobConnectorSinkConnectionFailService(FlinkLifecycleMapper mapper) {
        this.lifecycleMapper = mapper;
    }

    @Override
    public DecisionValueResult getDecisionValue(DeploymentDto deployment) {
        boolean sinkConnError = lifecycleMapper.countEvents(
                deployment.getDeploymentId(), deployment.getJobId(),
                "SINK_CONN_ERROR", 300000L) > 0;
        DecisionValueResult result = new DecisionValueResult();
        if (sinkConnError) {
            result.setValue(1);
        }
        return result;
    }
}
