# xdev R 冒烟：只用模拟数据。写出 sim.csv 和 lm_summary.txt，最后打印 XDEV_R_OK
set.seed(20261030)
n <- 200
x <- rnorm(n)
y <- 1.5 + 2 * x + rnorm(n, sd = 0.5)
d <- data.frame(id = seq_len(n), x = x, y = y)
write.csv(d, "sim.csv", row.names = FALSE)
fit <- lm(y ~ x, data = d)
writeLines(c(capture.output(print(R.version.string)), capture.output(summary(fit))), "lm_summary.txt")
cat(sprintf("XDEV_R_OK n=%d slope=%.4f intercept=%.4f\n", n, coef(fit)[["x"]], coef(fit)[["(Intercept)"]]))
