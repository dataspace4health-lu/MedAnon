import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;

public class HealthCheck {
    public static void main(String[] args) throws Exception {
        var client = HttpClient.newBuilder()
            .connectTimeout(Duration.ofSeconds(5))
            .build();
        var req = HttpRequest.newBuilder()
            .uri(URI.create("http://localhost:8080/actuator/health"))
            .timeout(Duration.ofSeconds(9))
            .build();
        var res = client.send(req, HttpResponse.BodyHandlers.discarding());
        System.exit(res.statusCode() == 200 ? 0 : 1);
    }
}
