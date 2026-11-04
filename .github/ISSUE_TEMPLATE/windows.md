---
name: Windows 上装不上 / 没反应
about: 实验性平台的问题反馈。带上 doctor 的输出，我们才有可能修。
title: "[Windows] "
labels: windows
---

## 先做这一件

在你的机器上跑下面这条（PowerShell 或 CMD 都行），把**整段输出**贴到下面的代码块里：

```
python "%USERPROFILE%\.claude\skills\claude-tuner\scripts\bootstrap.py" doctor
```

它不会打印任何 API Key（只回答「填了没」）。没有这段，我们能拿到的线索就只有「不行」两个字。

<details>
<summary>如果连这条都跑不起来（插件可能没装上）</summary>

把这三条的输出也贴上：

```
python --version
where python
dir "%USERPROFILE%\.claude\skills"
```
</details>

## doctor 输出

```
（粘贴在这里）
```

## 卡在哪一步

- [ ] 安装那行命令就跑不过
- [ ] 装完了，但配置页打不开
- [ ] 配置页能打开，但客户端发消息没反应
- [ ] 客户端有回应，但模型 / 上游不对
- [ ] 其它（下面写）

## 环境

- Windows 版本（`winver` 里那句）：
- 客户端版本（Claude 桌面端「关于」里的版本号）：
- 是否在代理 / VPN / 公司网络下：

## 你试过什么

（重装、重启客户端、跑过 `manager.py ensure` ……写了什么就是什么，没试过的不用编）
