package com.alibaba.flink.advisor.decision;

/** 决策结果对象: value=1 表示命中该故障模式。 */
public class DecisionValueResult {

    private int value;

    public int getValue() {
        return value;
    }

    public void setValue(int value) {
        this.value = value;
    }
}
