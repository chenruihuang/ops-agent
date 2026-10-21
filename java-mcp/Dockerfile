# 多阶段构建：Maven 层负责编译，JRE 层只带运行时。
# 分层的意义不只是体积 —— 编译依赖（几百 MB）永远不进最终镜像，
# 也就不存在"SNAPSHOT 依赖泄漏到运行环境"的问题。
FROM maven:3.9-eclipse-temurin-17 AS build
WORKDIR /build

# 先只拷 pom 并预拉依赖：改 Java 代码时这一层仍然命中缓存，
# 重建从"每次 3 分钟"降到"每次 30 秒"
COPY pom.xml .
RUN mvn -q -B dependency:go-offline

COPY src ./src
RUN mvn -q -B package -DskipTests

FROM eclipse-temurin:17-jre
WORKDIR /app

# 非 root 运行：容器逃逸的成本不至于为零
RUN addgroup --system app && adduser --system --ingroup app app
COPY --from=build /build/target/*.jar app.jar
USER app

EXPOSE 8080

# file.encoding 显式指定：Java 17 的默认编码仍受 locale 影响，
# 容器里 locale 是 POSIX 时会退回 ASCII，中文直接变成 ???
ENTRYPOINT ["java", "-Dfile.encoding=UTF-8", "-XX:MaxRAMPercentage=75", "-jar", "/app/app.jar"]
