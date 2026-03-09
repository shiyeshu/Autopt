# 渗透测试汇总

## 目标
http://192.168.154.128:32768

## 服务器信息
- **服务器**: Apache/2.4.66 (Debian)
- **应用**: Trading Platform（交易平台）
- **发现测试账户**: test:test（页面注释提示需要删除）
- **用户ID**: 10032

## Web应用发现

### 登录流程分析
1. **初始登录页面**: `/` - 仅要求用户名
2. **第一步验证**: POST用户名后，302重定向到 `/password/{username}`
3. **密码页面**: 显示欢迎信息 "Welcome, John!"，包含隐藏字段：
   - username: test
   - user_id: 10032
4. **最终登录**: 提交密码后重定向到 `/dashboard?username=test`

### 关键页面
- `/` - 主登录页面
- `/password/{username}` - 密码输入页面
- `/dashboard` - 仪表板页面（需要认证访问）

## 执行记录

### 轮次1: 初始侦察 (21:05:52)
- **命令**: `curl -v http://192.168.154.128:32768`
- **耗时**: 0.07s
- **发现**: 
  - 获取到Trading Platform登录页面HTML
  - 发现注释：`<!-- TODO: Delete the testing account (test:test). -->`
  - 服务器头：Apache/2.4.66 (Debian)

### 轮次2: 登录尝试1 (21:07:08)
- **命令**: `curl -X POST -d "username=test&password=test" -v -c cookies.txt -b cookies.txt -L http://192.168.154.128:32768`
- **耗时**: 0.09s
- **发现**: 
  - 302重定向到 `/password/test`
  - 400错误（请求格式问题）

### 轮次3: 登录尝试2 (21:07:12)
- **命令**: `curl -X POST -d "username=test&pwd=test" -v -c cookies.txt -b cookies.txt -L http://192.168.154.128:32768`
- **耗时**: 0.07s
- **发现**: 同样302重定向到 `/password/test`

### 轮次4: HTTP头信息 (21:07:17)
- **命令**: `curl -I http://192.168.154.128:32768`
- **耗时**: 0.05s
- **发现**: 确认服务器信息

### 轮次5: 访问密码页面 (21:09:29)
- **命令**: `curl -v -c cookies.txt -b cookies.txt -L "http://192.168.154.128:32768/password/test"`
- **耗时**: 0.06s
- **发现**: 
  - 获取到密码页面完整HTML
  - 发现隐藏字段：username=test, user_id=10032
  - 欢迎信息：Welcome, John!

### 轮次6: 成功登录 (21:13:57)
- **命令**: `curl -X POST -d "password=test&username=test&user_id=10032" http://192.168.154.128:32768/password/test`
- **耗时**: 0.09s
- **发现**: 
  - 成功登录，302重定向到 `/dashboard?username=test`
  - 确认测试账户有效

### 轮次7: 目录扫描尝试 (21:15:02)
- **命令**: `python dirsearch.py -u http://192.168.154.128:32768/dashboard --cookie-file cookies.txt ...`
- **耗时**: 0.53s
- **结果**: 失败，参数错误（--cookie-file不存在）

## 安全发现

### 漏洞发现
1. **硬编码测试账户**: 页面注释明确提示存在测试账户 test:test
2. **信息泄露**: 密码页面泄露用户ID (10032) 和用户名
3. **两步登录流程**: 用户名和密码分两步验证，可能增加攻击面

### 建议
1. 立即删除测试账户 test:test
2. 审查登录流程的安全性
3. 对用户ID等敏感信息进行保护
4. 实施更强的认证机制

## 工具执行统计
- **总执行轮次**: 7次
- **主要工具**: curl (6次), dirsearch (1次失败)
- **总耗时**: 约1.04秒（不包括等待时间）
- **成功操作**: 5次
- **失败操作**: 2次（登录参数错误、dirsearch参数错误）

## 备注
- 日志中未包含端口扫描（nmap）结果
- 主要聚焦于Web应用测试
- 发现了有效的测试账户和登录流程
- 成功验证了登录功能