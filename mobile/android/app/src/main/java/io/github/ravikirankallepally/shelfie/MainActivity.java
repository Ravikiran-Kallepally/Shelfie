package io.github.ravikirankallepally.shelfie;

import android.webkit.CookieManager;

import com.getcapacitor.BridgeActivity;

public class MainActivity extends BridgeActivity {

    @Override
    public void onPause() {
        super.onPause();
        // The WebView writes cookies to disk on a timer. Save them now, so swiping the app
        // away right after signing in doesn't sign you out.
        CookieManager.getInstance().flush();
    }
}
