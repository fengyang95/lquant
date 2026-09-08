"""定时同步：后台常驻调度。

用法：
    from lquant.sync import manager as sync
    sync.seed_defaults()          # 种子默认作业（幂等）
    sync.loop_forever()           # 常驻循环（放 daemon 线程）
"""
