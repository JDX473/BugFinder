package com.alibaba.flink.advisor.decision;

/** 决策服务统一接口。 */
public interface RuleDecision {

    DecisionValueResult getDecisionValue(DeploymentDto deployment);
}
