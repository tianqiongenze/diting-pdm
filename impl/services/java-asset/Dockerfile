FROM 10.167.2.175:30500/base/eclipse-temurin:25-jre-alpine
WORKDIR /app
COPY target/pdm-java-asset.jar /app/app.jar
RUN addgroup -S pdm && adduser -S pdm -G pdm 2>/dev/null || true
USER pdm
EXPOSE 8081
ENTRYPOINT ["java","-XX:MaxRAMPercentage=75","-jar","/app/app.jar"]
