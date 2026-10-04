using UnityEngine;
using Unity.Robotics.ROSTCPConnector;
using RosMessageTypes.Std;

// P0 smoke test: publishes "hello" on /twin/state once a second.
public class HelloPublisher : MonoBehaviour {
    ROSConnection ros; float timer;
    void Start() { ros = ROSConnection.GetOrCreateInstance(); ros.RegisterPublisher<StringMsg>("/twin/state"); }
    void Update() { timer += Time.deltaTime; if (timer > 1f) { timer = 0f; ros.Publish("/twin/state", new StringMsg("hello")); } }
}
