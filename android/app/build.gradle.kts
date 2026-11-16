plugins {
    id("com.android.application")
}

android {
    namespace = "org.prontofix.app"
    compileSdk = 35

    defaultConfig {
        applicationId = "org.prontofix.app"
        minSdk = 21
        targetSdk = 35
        versionCode = 4
        versionName = "3.1.0"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_1_8
        targetCompatibility = JavaVersion.VERSION_1_8
    }
}

dependencies {
    implementation("androidx.room:room-runtime:2.6.1")
    annotationProcessor("androidx.room:room-compiler:2.6.1")
    implementation("androidx.work:work-runtime:2.9.1")
}
