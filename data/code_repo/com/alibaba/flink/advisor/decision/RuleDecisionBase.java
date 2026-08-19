package com.alibaba.flink.advisor.decision;

/**
 * 决策基础类: 实现 RuleDecision 接口,为所有决策服务提供公共逻辑
 * (事件查询窗口、结果对象构造等)。
 */
public abstract class RuleDecisionBase implements RuleDecision {

    protected static final long DEFAULT_EVENT_WINDOW_MS = 300000L;

    public DecisionValueResult emptyResult() {
        return new DecisionValueResult();
    }
}
